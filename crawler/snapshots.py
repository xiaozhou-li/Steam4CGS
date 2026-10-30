"""Longitudinal monthly engagement snapshots: one record per (game, collection month).

Snapshots are point-in-time observations made **during** their month. The
collector refuses any month other than the current UTC month, so a past
month can never be filled in afterwards (no invented history). Re-running
within the month is idempotent: complete records are kept, and only sources
that failed are fetched again.

Every source response is appended to an audit table (URL, UTC time,
status, trimmed raw JSON). The audit table is also the within-month cache.

Indicators and sources:

* ``review_*``: official ``IUserReviewsService/GetAppReviews`` summary, verified
  exactly as in crawler.reviews. Cumulative counts. **Not** a measure of player
  retention.
* ``current_player_count``: official ``ISteamUserStats/GetNumberOfCurrentPlayers``.
  A single point-in-time concurrency reading, not monthly active users.
* ``age_months``, ``price_usd_cents``, ``achievement_count``: Store ``appdetails``.
* ``early_access_current``: GetItems ``is_early_access`` plus the genre
  (crawler.metadata.early_access_evidence).
* ``recent_review_count``: always blank. No official *summary* method reports a
  recent-window review count, and paging through individual reviews is out of
  scope.
"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import io
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crawler import achievements as ach
from crawler import reviews as rv
from crawler.discovery import atomic_write_bytes
from crawler.discovery_state import iso
from crawler.metadata import EA_FALSE, EA_TRUE, SourceResult, early_access_evidence
from crawler.release_dates import parse_release_date
from crawler.steam_api import (
    APPDETAILS_CACHED_FIELDS,
    SteamStoreAPI,
    SteamWebAPI,
    appdetails_request,
    current_players_request,
    public_url,
    store_items_request,
)
from crawler.transport import RequestBudgetExceeded, SteamAuthError, SteamError, SteamHTTPError, SteamRateLimitError

SNAPSHOT_CSV = "steam_game_monthly_snapshots.csv"
ANALYSIS_CSV = "steam_game_monthly_snapshots_analysis.csv"
ANALYSIS_SCHEMA = "steam_game_monthly_snapshots_analysis.schema.json"

SNAPSHOT_FIELDS = (
    "appid", "snapshot_month", "snapshot_utc", "age_months", "review_total", "review_positive", "review_negative",
    "review_positive_share", "recent_review_count", "current_player_count", "achievement_count", "price_usd_cents",
    "early_access_current",
)
INDICATORS = ("age_months", "reviews", "recent_review_count", "current_player_count", "achievement_count",
              "price_usd_cents", "early_access_current")
SOURCES = ("appdetails", "store_item", "current_players", "reviews")

# Observation / indicator statuses
OK = "ok"
MISSING = "missing"              # the source answered, but has no value
NOT_AVAILABLE = "not_available"  # 4xx, success=false, method not visible, or not offered by any official summary
UNRECOGNIZED = "unrecognized"    # the response does not have the documented shape
ERROR = "error"                  # request failed after retries (retried later this month)
NOT_ATTEMPTED = "not_attempted"  # e.g. reviews without access to GetAppReviews

RECENT_REVIEWS_NOTE = ("no official summary method reports a recent-window review count; "
                       "counting individual reviews is out of scope")


def month_of(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m")


def _parse_ts(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def age_in_months(release_raw: str | None, coming_soon: bool | None, at: dt.date) -> int | None:
    """Whole months from an exact release date to ``at``; None when unknown or not yet released."""
    if coming_soon is True:
        return None
    parsed = parse_release_date(release_raw)
    if parsed is None or parsed.exact is None:
        return None
    r = parsed.exact
    months = (at.year - r.year) * 12 + (at.month - r.month) - (1 if at.day < r.day else 0)
    return months if months >= 0 else None


# --------------------------------------------------------------------------
# State: append-only observations (audit trail + cache), records, runs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    obs_id: int
    month: str
    appid: int
    source: str
    status: str
    url: str
    retrieved_at: str
    raw: Any
    detail: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    obs_id INTEGER PRIMARY KEY AUTOINCREMENT, month TEXT NOT NULL, appid INTEGER NOT NULL, source TEXT NOT NULL,
    status TEXT NOT NULL, url TEXT NOT NULL, retrieved_at TEXT NOT NULL, raw_json TEXT NOT NULL,
    detail TEXT NOT NULL, run_id INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS obs_key ON observations (month, appid, source);
CREATE TABLE IF NOT EXISTS records (
    month TEXT NOT NULL, appid INTEGER NOT NULL, snapshot_utc TEXT NOT NULL, values_json TEXT NOT NULL,
    statuses_json TEXT NOT NULL, complete INTEGER NOT NULL, obs_ids TEXT NOT NULL, PRIMARY KEY (month, appid));
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, month TEXT NOT NULL, scope TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT, requested INTEGER, collected INTEGER, pending INTEGER, stop_reason TEXT, requests_sent INTEGER);
"""


class SnapshotState:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._db:
            self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> SnapshotState:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def add_observation(self, month: str, appid: int, source: str, status: str, url: str, retrieved_at: str,
                        raw: Any, detail: str, run_id: int) -> Observation:
        with self._db:
            cur = self._db.execute(
                "INSERT INTO observations (month, appid, source, status, url, retrieved_at, raw_json, detail, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (month, appid, source, status, url, retrieved_at, json.dumps(raw, ensure_ascii=False, sort_keys=True),
                 detail, run_id))
        return Observation(cur.lastrowid, month, appid, source, status, url, retrieved_at, raw, detail)

    def latest_observations(self, month: str, appid: int) -> dict[str, Observation]:
        rows = self._db.execute(
            "SELECT obs_id, month, appid, source, status, url, retrieved_at, raw_json, detail FROM observations "
            "WHERE month = ? AND appid = ? ORDER BY obs_id", (month, appid))
        return {r[3]: Observation(r[0], r[1], r[2], r[3], r[4], r[5], r[6], json.loads(r[7]), r[8]) for r in rows}

    def all_observations(self) -> Iterable[tuple]:
        return self._db.execute("SELECT obs_id, month, appid, source, status, url, retrieved_at, raw_json, detail, "
                                "run_id FROM observations ORDER BY obs_id")

    def save_record(self, month: str, appid: int, snapshot_utc: str, values: Mapping[str, str],
                    statuses: Mapping[str, str], complete: bool, obs_ids: Sequence[int]) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(month, appid) DO UPDATE SET "
                "snapshot_utc = excluded.snapshot_utc, values_json = excluded.values_json, "
                "statuses_json = excluded.statuses_json, complete = excluded.complete, obs_ids = excluded.obs_ids",
                (month, appid, snapshot_utc, json.dumps(values, sort_keys=True), json.dumps(statuses, sort_keys=True),
                 int(complete), json.dumps(list(obs_ids))))

    def records(self, month: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT month, appid, snapshot_utc, values_json, statuses_json, complete, obs_ids FROM records"
        rows = self._db.execute(q + (" WHERE month = ?" if month else "") + " ORDER BY month, appid",
                                (month,) if month else ())
        return [{"month": r[0], "appid": r[1], "snapshot_utc": r[2], "values": json.loads(r[3]),
                 "statuses": json.loads(r[4]), "complete": bool(r[5]), "obs_ids": json.loads(r[6])} for r in rows]

    def start_run(self, month: str, scope: str, started_at: str) -> int:
        with self._db:
            return self._db.execute("INSERT INTO runs (month, scope, started_at) VALUES (?, ?, ?)",
                                    (month, scope, started_at)).lastrowid

    def finish_run(self, run_id: int, finished_at: str, **counts: Any) -> None:
        with self._db:
            self._db.execute("UPDATE runs SET finished_at = ?, requested = ?, collected = ?, pending = ?, "
                             "stop_reason = ?, requests_sent = ? WHERE run_id = ?",
                             (finished_at, counts["requested"], counts["collected"], counts["pending"],
                              counts["stop_reason"], counts["requests_sent"], run_id))

    def runs(self) -> list[tuple]:
        return list(self._db.execute("SELECT run_id, month, scope, started_at, finished_at, requested, collected, "
                                     "pending, stop_reason, requests_sent FROM runs ORDER BY run_id"))


# --------------------------------------------------------------------------
# Deriving one record from its observations (pure)
# --------------------------------------------------------------------------


def _share(pos: int, total: int) -> str:
    return ach.fmt(pos / total) if total else ""


def derive_record(appid: int, obs: Mapping[str, Observation], at: dt.date) -> tuple[dict[str, str], dict[str, str]]:
    """(values, statuses) for the snapshot row. Values are CSV cells; '' = unknown."""
    values = {k: "" for k in SNAPSHOT_FIELDS if k not in ("appid", "snapshot_month", "snapshot_utc")}
    statuses: dict[str, str] = {}
    details = obs.get("appdetails")
    data = details.raw if details is not None and details.status == OK else None

    # age_months, price, achievements: appdetails
    if details is None or details.status != OK:
        st = details.status if details else ERROR
        for k in ("age_months", "price_usd_cents", "achievement_count"):
            statuses[k] = st
    else:
        release = data.get("release_date") if isinstance(data.get("release_date"), Mapping) else {}
        age = age_in_months(release.get("date"), release.get("coming_soon"), at)
        values["age_months"] = "" if age is None else str(age)
        statuses["age_months"] = OK if age is not None else MISSING

        po = data.get("price_overview") if isinstance(data.get("price_overview"), Mapping) else None
        if data.get("is_free") is True:
            values["price_usd_cents"], statuses["price_usd_cents"] = "0", OK
        elif po and po.get("currency") == "USD" and isinstance(po.get("initial"), int) and po["initial"] >= 0:
            values["price_usd_cents"], statuses["price_usd_cents"] = str(po["initial"]), OK  # regular, not sale
        elif po and po.get("currency") not in (None, "USD"):
            statuses["price_usd_cents"] = UNRECOGNIZED
        else:
            statuses["price_usd_cents"] = MISSING

        a_status, total, _, _ = ach.parse_store(data)
        if a_status in (ach.OK, ach.EMPTY) and total is not None:
            values["achievement_count"], statuses["achievement_count"] = str(total), OK
        else:
            statuses["achievement_count"] = MISSING if a_status in (ach.NO_DATA, ach.OK) else UNRECOGNIZED

    # early access: store item + genres
    item = obs.get("store_item")
    if details is None or details.status == ERROR:
        statuses["early_access_current"] = ERROR
    else:
        genres = [g.get("description") for g in (data or {}).get("genres") or [] if isinstance(g, Mapping)]
        ad_src = SourceResult("appdetails", details.url, details.retrieved_at, data=data)
        item_src = None if item is None else SourceResult(
            "store_items", item.url, item.retrieved_at, data=item.raw if item.status == OK else None,
            error=(item.detail or "error") if item.status == ERROR else None)
        ea = early_access_evidence(appid, [g for g in genres if isinstance(g, str)] or None, ad_src, item_src)
        values["early_access_current"] = {EA_TRUE: "TRUE", EA_FALSE: "FALSE"}.get(ea.status, "")
        statuses["early_access_current"] = (OK if ea.status in (EA_TRUE, EA_FALSE) else
                                            ERROR if item is not None and item.status == ERROR else MISSING)

    # current players
    players = obs.get("current_players")
    if players is not None and players.status == OK:
        values["current_player_count"], statuses["current_player_count"] = str(players.raw["player_count"]), OK
    else:
        statuses["current_player_count"] = players.status if players else ERROR

    # reviews
    reviews = obs.get("reviews")
    if reviews is not None and reviews.status == OK:
        r = reviews.raw
        values.update(review_total=str(r["total_reviews"]), review_positive=str(r["total_positive"]),
                      review_negative=str(r["total_negative"]),
                      review_positive_share=_share(r["total_positive"], r["total_reviews"]))
        statuses["reviews"] = OK
    else:
        statuses["reviews"] = reviews.status if reviews else NOT_ATTEMPTED

    statuses["recent_review_count"] = NOT_AVAILABLE
    return values, statuses


# --------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------


class SnapshotMonthError(ValueError):
    """The requested month is not the current UTC month (history cannot be collected afterwards)."""


@dataclass
class SnapshotReport:
    month: str
    requested: int = 0
    collected: int = 0
    pending: int = 0
    stop_reason: str = "complete"
    reviews_available: bool = False
    reviews_note: str = ""
    statuses: dict[str, dict[str, int]] = field(default_factory=dict)


class SnapshotCollector:
    def __init__(self, state: SnapshotState, web: SteamWebAPI, store: SteamStoreAPI, *,
                 review_settings: rv.ReviewSettings = rv.ReviewSettings(),
                 now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
                 log: Callable[[str], None] = print):
        self.state = state
        self.web = web
        self.store = store
        self.review_settings = review_settings
        self._now = now
        self._log = log
        self._review_params: dict[str, Any] | None = None

    def _review_access(self) -> tuple[bool, str]:
        try:
            methods = self.web.get_supported_methods()
        except SteamError as exc:
            return False, f"GetSupportedAPIList failed: {exc}"
        m = methods.get((rv.INTERFACE, rv.METHOD, rv.VERSION))
        if m is None:
            return False, (f"{rv.INTERFACE}/{rv.METHOD}/v{rv.VERSION} not visible"
                           + ("" if self.web.has_key else " (no STEAM_API_KEY)"))
        self._review_params = {k: v for k, v in self.review_settings.desired_params().items() if k in m.parameters}
        return True, ""

    def _fetch(self, month: str, appid: int, source: str, run_id: int, refresh: bool) -> Observation:
        at = lambda: iso(self._now())  # noqa: E731
        try:
            if source == "appdetails":
                url = public_url(*appdetails_request(appid, self.store.country, self.store.language))
                fetched = self.store.fetch_app_details(appid)
                if refresh or month_of(_parse_ts(fetched.retrieved_at)) != month:
                    fetched = self.store.fetch_app_details(appid, refresh=True)  # only this month's data counts
                if fetched.data is None:
                    return self.state.add_observation(month, appid, source, NOT_AVAILABLE, fetched.url,
                                                      fetched.retrieved_at, None, "appdetails success=false", run_id)
                raw = {k: fetched.data[k] for k in APPDETAILS_CACHED_FIELDS if k in fetched.data}
                return self.state.add_observation(month, appid, source, OK, fetched.url, fetched.retrieved_at, raw,
                                                  "from cache" if fetched.from_cache else "", run_id)
            if source == "store_item":
                url = public_url(*store_items_request(appid, self.store.country, self.store.language))
                item = self.web.get_store_item(appid, country=self.store.country, language=self.store.language)
                if item is None:
                    return self.state.add_observation(month, appid, source, NOT_AVAILABLE, url, at(), None,
                                                      "no visible store item", run_id)
                raw = {k: item[k] for k in ("appid", "success", "visible", "is_early_access") if k in item}
                return self.state.add_observation(month, appid, source, OK, url, at(), raw, "", run_id)
            if source == "current_players":
                url = public_url(*current_players_request(appid))
                body = self.web.get_number_of_current_players(appid)
                count = body.get("player_count")
                if body.get("result") != 1:
                    return self.state.add_observation(month, appid, source, NOT_AVAILABLE, url, at(), body,
                                                      f"result={body.get('result')!r}", run_id)
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    return self.state.add_observation(month, appid, source, UNRECOGNIZED, url, at(), body,
                                                      f"player_count={count!r}", run_id)
                return self.state.add_observation(month, appid, source, OK, url, at(), body, "", run_id)
            if source == "reviews":
                params = self._review_params or {}
                url = public_url(rv.ENDPOINT_URL, {"appid": appid, **params})
                v = rv.verify_summary(self.web.get_app_reviews(appid, **params))
                raw = {"status": v.status, "review_score_desc": v.review_score_desc, "total_reviews": v.total_reviews,
                       "total_positive": v.total_positive, "total_negative": v.total_negative,
                       "review_language": self.review_settings.language,
                       "purchase_type": self.review_settings.purchase_type}
                # Verified counts are usable whatever the label; an inconsistent summary is not.
                status = OK if v.status in (rv.OK, rv.NO_REVIEWS, rv.MISSING_LABEL, rv.UNRECOGNIZED_LABEL) else UNRECOGNIZED
                return self.state.add_observation(month, appid, source, status, url, at(), raw, v.detail, run_id)
        except (SteamRateLimitError, RequestBudgetExceeded):
            raise
        except SteamHTTPError as exc:
            status = NOT_AVAILABLE if 400 <= exc.status < 500 and not isinstance(exc, SteamAuthError) else ERROR
            return self.state.add_observation(month, appid, source, status, url, at(), None,
                                              f"HTTP {exc.status}: {exc}"[:300], run_id)
        except SteamError as exc:
            return self.state.add_observation(month, appid, source, ERROR, url, at(), None,
                                              f"{type(exc).__name__}: {exc}"[:300], run_id)
        raise ValueError(source)

    def run(self, appids: Iterable[int], *, month: str, scope: str = "", limit: int | None = None,
            refresh: bool = False) -> SnapshotReport:
        current = month_of(self._now())
        if month != current:
            raise SnapshotMonthError(f"snapshots can only be collected for the current UTC month ({current}); "
                                     f"{month} would be {'a past' if month < current else 'a future'} observation")
        report = SnapshotReport(month)
        wanted = list(dict.fromkeys(appids))
        report.requested = len(wanted)
        existing = {r["appid"]: r for r in self.state.records(month)}
        todo = [a for a in wanted if refresh or a not in existing or not existing[a]["complete"]]
        self._log(f"snapshot {month}: {len(todo)} of {len(wanted)} game(s) to collect")
        run_id = self.state.start_run(month, scope, iso(self._now()))
        requests_before = self.web.transport.requests_sent
        if todo:
            report.reviews_available, report.reviews_note = self._review_access()
        try:
            for appid in todo:
                if limit is not None and report.collected >= limit:
                    report.stop_reason = "limit_reached"
                    break
                if month_of(self._now()) != month:
                    report.stop_reason = "month_ended"  # never write next month's data into this month
                    break
                obs = self.state.latest_observations(month, appid)
                for source in SOURCES:
                    reuse = source in obs and obs[source].status != ERROR and not (refresh and appid in existing)
                    if reuse:
                        continue
                    if source == "reviews" and not report.reviews_available:
                        obs[source] = self.state.add_observation(
                            month, appid, source, NOT_ATTEMPTED, rv.ENDPOINT_URL, iso(self._now()), None,
                            report.reviews_note, run_id)
                        continue
                    obs[source] = self._fetch(month, appid, source, run_id, refresh)
                if any(month_of(_parse_ts(o.retrieved_at)) != month for o in obs.values()):
                    # The month ended while this game was being collected: keep the observations in the
                    # audit trail (with their true times) but never file them under the previous month.
                    report.stop_reason = "month_ended"
                    break
                snapshot_utc = max(o.retrieved_at for o in obs.values())
                values, statuses = derive_record(appid, obs, _parse_ts(snapshot_utc).date())
                complete = all(o.status != ERROR for o in obs.values())
                self.state.save_record(month, appid, snapshot_utc, values, statuses, complete,
                                       [o.obs_id for o in obs.values()])
                report.collected += 1
        except SteamRateLimitError as exc:
            report.stop_reason = "rate_limited"
            self._log(f"stopping: {exc}")
        except RequestBudgetExceeded:
            report.stop_reason = "budget_exhausted"
        finally:
            records = {r["appid"]: r for r in self.state.records(month)}
            report.pending = sum(1 for a in wanted if a not in records or not records[a]["complete"])
            report.statuses = {k: dict(collections.Counter(records[a]["statuses"].get(k, "") for a in wanted
                                                           if a in records)) for k in INDICATORS}
            self.state.finish_run(run_id, iso(self._now()), requested=report.requested, collected=report.collected,
                                  pending=report.pending, stop_reason=report.stop_reason,
                                  requests_sent=self.web.transport.requests_sent - requests_before)
        return report


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------


def _csv(fields: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


ANALYSIS_COLUMNS: dict[str, tuple[str, str]] = {
    "appid": ("Int64", "Steam AppID"),
    "snapshot_month": ("string", "Collection month, YYYY-MM (UTC)"),
    "snapshot_utc": ("datetime64[ns, UTC]", "Latest source retrieval time of this record"),
    "dataset": ("string", "'new' (steamproducts2020_2026_new.csv), 'legacy' (steamproducts2020.csv) or 'other'"),
    "snapshot_index": ("Int64", "0 for the game's first snapshot, then 1, 2, ... (missing months are not filled in)"),
    "months_since_previous_snapshot": ("Int64", "Calendar months since the game's previous snapshot; blank for the first"),
    "age_months": ("Int64", "Whole months since the exact store release date at snapshot time"),
    "review_total": ("Int64", "Cumulative review count (verified GetAppReviews summary)"),
    "review_positive": ("Int64", "Cumulative positive reviews"),
    "review_negative": ("Int64", "Cumulative negative reviews"),
    "review_positive_share": ("float64", "review_positive / review_total"),
    "review_total_change": ("Int64", "Net change in cumulative review_total since the previous snapshot; not new "
                                     "players and not retention"),
    "recent_review_count": ("Int64", "Always blank: no official summary method reports it"),
    "current_player_count": ("Int64", "Concurrent players at the moment of the request (GetNumberOfCurrentPlayers)"),
    "achievement_count": ("Int64", "Store achievement total (appdetails)"),
    "price_usd_cents": ("Int64", "Regular US listing price in cents; 0 = free; sale prices excluded"),
    "early_access_current": ("boolean", "In Early Access at snapshot time: true/false; blank = unknown"),
    "record_complete": ("boolean", "false when a source failed this month (fields stay blank)"),
    "age_months_status": ("string", "Status of age_months"),
    "reviews_status": ("string", "Status of the review fields"),
    "recent_review_count_status": ("string", "Always not_available"),
    "current_player_count_status": ("string", "Status of current_player_count"),
    "achievement_count_status": ("string", "Status of achievement_count"),
    "price_usd_cents_status": ("string", "Status of price_usd_cents"),
    "early_access_current_status": ("string", "Status of early_access_current"),
}


def _months_between(a: str, b: str) -> int:
    ya, ma = map(int, a.split("-"))
    yb, mb = map(int, b.split("-"))
    return (yb - ya) * 12 + (mb - ma)


def export(state: SnapshotState, base: Path, work: Path, *, new_ids: Iterable[int] = (),
           legacy_ids: Iterable[int] = ()) -> dict[str, int]:
    """Write the snapshot table, the analysis-ready panel (+ dtype schema) and the audit trail."""
    records = state.records()
    new_ids, legacy_ids = set(new_ids), set(legacy_ids)
    rows = [{"appid": r["appid"], "snapshot_month": r["month"], "snapshot_utc": r["snapshot_utc"], **r["values"]}
            for r in records]
    atomic_write_bytes(base / SNAPSHOT_CSV, _csv(SNAPSHOT_FIELDS, rows))

    by_game: dict[int, list[dict]] = collections.defaultdict(list)
    for r in records:
        by_game[r["appid"]].append(r)
    panel = []
    for appid in sorted(by_game):
        prev = None
        for i, r in enumerate(sorted(by_game[appid], key=lambda x: x["month"])):
            v, s = r["values"], r["statuses"]
            change = ""
            if prev and prev["values"]["review_total"] and v["review_total"]:
                change = str(int(v["review_total"]) - int(prev["values"]["review_total"]))
            panel.append({
                "appid": appid, "snapshot_month": r["month"], "snapshot_utc": r["snapshot_utc"],
                "dataset": "new" if appid in new_ids else "legacy" if appid in legacy_ids else "other",
                "snapshot_index": i,
                "months_since_previous_snapshot": "" if prev is None else _months_between(prev["month"], r["month"]),
                **{k: v[k] for k in ("age_months", "review_total", "review_positive", "review_negative",
                                     "review_positive_share")},
                "review_total_change": change, "recent_review_count": v["recent_review_count"],
                "current_player_count": v["current_player_count"], "achievement_count": v["achievement_count"],
                "price_usd_cents": v["price_usd_cents"],
                "early_access_current": {"TRUE": "true", "FALSE": "false"}.get(v["early_access_current"], ""),
                "record_complete": "true" if r["complete"] else "false",
                "age_months_status": s.get("age_months", ""), "reviews_status": s.get("reviews", ""),
                "recent_review_count_status": s.get("recent_review_count", ""),
                "current_player_count_status": s.get("current_player_count", ""),
                "achievement_count_status": s.get("achievement_count", ""),
                "price_usd_cents_status": s.get("price_usd_cents", ""),
                "early_access_current_status": s.get("early_access_current", ""),
            })
            prev = r
    atomic_write_bytes(base / ANALYSIS_CSV, _csv(tuple(ANALYSIS_COLUMNS), panel))
    schema_doc = {"file": ANALYSIS_CSV, "grain": "one row per (appid, snapshot_month)",
                  "read_with": f"pandas.read_csv('{ANALYSIS_CSV}', dtype=..., parse_dates=['snapshot_utc'])",
                  "columns": {k: {"dtype": t, "description": d} for k, (t, d) in ANALYSIS_COLUMNS.items()}}
    atomic_write_bytes(base / ANALYSIS_SCHEMA, (json.dumps(schema_doc, indent=2) + "\n").encode())

    audit = [{"obs_id": o[0], "snapshot_month": o[1], "appid": o[2], "source": o[3], "status": o[4], "url": o[5],
              "retrieved_at": o[6], "raw_json": o[7], "detail": o[8], "run_id": o[9]} for o in state.all_observations()]
    atomic_write_bytes(work / "snapshots" / "snapshot_audit.csv",
                       _csv(("obs_id", "snapshot_month", "appid", "source", "status", "url", "retrieved_at",
                             "raw_json", "detail", "run_id"), audit))
    atomic_write_bytes(work / "snapshots" / "snapshot_runs.csv",
                       _csv(("run_id", "month", "scope", "started_at", "finished_at", "requested", "collected",
                             "pending", "stop_reason", "requests_sent"),
                            [dict(zip(("run_id", "month", "scope", "started_at", "finished_at", "requested",
                                       "collected", "pending", "stop_reason", "requests_sent"), r))
                             for r in state.runs()]))
    return {"records": len(rows), "games": len(by_game), "observations": len(audit)}


def coverage_markdown(state: SnapshotState, generated_at: str) -> str:
    records = state.records()
    months = sorted({r["month"] for r in records})
    lines = ["# Monthly snapshot coverage", "", f"Generated {generated_at} by `python -m crawler snapshot`.", "",
             "> Cumulative review counts and point-in-time player counts are **proxies** with known biases. They are",
             "> not measures of player retention or longevity (see `docs/snapshots.md`).", ""]
    for m in months:
        rs = [r for r in records if r["month"] == m]
        lines += [f"## {m}", "", f"{len(rs)} game(s); {sum(r['complete'] for r in rs)} complete.", "",
                  "| Indicator | ok | missing | not_available | unrecognized | error | not_attempted |",
                  "|---|---|---|---|---|---|---|"]
        for k in INDICATORS:
            c = collections.Counter(r["statuses"].get(k, "") for r in rs)
            lines.append(f"| `{k}` | " + " | ".join(str(c.get(s, 0)) for s in
                                                     (OK, MISSING, NOT_AVAILABLE, UNRECOGNIZED, ERROR, NOT_ATTEMPTED)) + " |")
        lines.append("")
    return "\n".join(lines) + "\n"

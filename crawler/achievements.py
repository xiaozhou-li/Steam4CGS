"""Achievement-system variables for Steam games.

Sources, per game:

* **Store summary**: ``appdetails.achievements.total`` and the ``Steam
  Achievements`` category (reused from the appdetails response cache).
* **ISteamUserStats/GetSchemaForGame/v2**: the official achievement list
  (needs a Steam Web API key; skipped without one).
* **ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2**: global unlock
  percentages (no key needed).

Unavailable data is never turned into zero. ``has_steam_achievements`` is
FALSE only when an official source explicitly reports none (an empty schema
or an empty percentage list) and nothing contradicts it. A missing field,
missing category, HTTP 4xx or failed request leaves it blank (unknown).

Global unlock percentages measure **observed rarity**: the share of Steam
accounts owning the game that unlocked the achievement. They depend on who
bought and played the game, for how long, and on hidden or optional
achievements. They are **not** a measure of intrinsic difficulty.
"""

from __future__ import annotations

import collections
import csv
import dataclasses
import datetime as dt
import io
import json
import math
import sqlite3
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crawler import schema
from crawler.discovery import atomic_write_bytes
from crawler.discovery_state import iso
from crawler.steam_api import (
    SteamStoreAPI,
    SteamWebAPI,
    appdetails_request,
    global_achievement_percentages_request,
    public_url,
    schema_for_game_request,
)
from crawler.transport import RequestBudgetExceeded, SteamError, SteamHTTPError, SteamRateLimitError

FEATURES_CSV = "steam_achievements_features.csv"
PER_ACHIEVEMENT_CSV = "steam_achievements_per_achievement.csv"
FEATURE_FIELDS = (
    "appid", "snapshot_utc", "has_steam_achievements", "achievement_count", "achievement_count_source",
    "global_unlock_percentages_available", "mean_global_unlock_pct", "median_global_unlock_pct",
    "min_global_unlock_pct", "share_below_10pct", "share_below_1pct",
)
PER_ACHIEVEMENT_FIELDS = (
    "appid", "snapshot_utc", "achievement_api_name", "display_name", "hidden", "in_schema",
    "global_unlock_pct", "pct_source",
)
SOURCE_FIELDS = (
    "appid", "collected_at", "store_status", "store_total", "store_category", "store_url", "store_retrieved_at",
    "schema_status", "schema_count", "schema_url", "schema_retrieved_at", "pct_status", "pct_count", "pct_url",
    "pct_retrieved_at", "conflict", "detail",
)
ACHIEVEMENTS_CATEGORY = "Steam Achievements"

# Source statuses
OK = "ok"                        # the source answered with achievements
EMPTY = "empty"                  # the source explicitly reports no achievements
NO_DATA = "no_data"              # the source answered, but says nothing either way
NOT_AVAILABLE = "not_available"  # HTTP 4xx / success=false: no data for this app here
ERROR = "error"                  # request failed after retries (retried next run)
UNRECOGNIZED = "unrecognized"    # the response does not have the documented shape
NOT_ATTEMPTED = "not_attempted"  # e.g. GetSchemaForGame without a key
RETRY_STATUSES = frozenset({ERROR})

COUNT_SOURCES = ("schema", "global_percentages", "store_summary")


# --------------------------------------------------------------------------
# Source outcomes and parsing (pure)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    status: str
    url: str = ""
    retrieved_at: str = ""
    count: int | None = None
    items: tuple = ()            # schema: (name, display, hidden); percentages: (name, pct)
    category: bool | None = None  # store only
    detail: str = ""


def parse_store(data: Mapping[str, Any] | None) -> tuple[str, int | None, bool | None, str]:
    """(status, total, has_category, detail) from appdetails ``data`` (``None`` = success:false)."""
    if data is None:
        return NOT_AVAILABLE, None, None, "appdetails success=false"
    categories = data.get("categories")
    labels = {c.get("description") for c in categories if isinstance(c, dict)} if isinstance(categories, list) else None
    category = None if labels is None else ACHIEVEMENTS_CATEGORY in labels
    ach = data.get("achievements")
    if ach is None:
        # Absent summary: unknown, unless the category says there are achievements.
        return (OK if category else NO_DATA), None, category, "no achievements summary in appdetails"
    total = ach.get("total") if isinstance(ach, Mapping) else None
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return UNRECOGNIZED, None, category, f"achievements.total is {total!r}"
    return (OK if total > 0 else EMPTY), total, category, ""


def parse_schema(body: Mapping[str, Any]) -> tuple[str, tuple, str]:
    """(status, ((api_name, display_name, hidden), ...), detail) from GetSchemaForGame."""
    game = body.get("game")
    if not isinstance(game, Mapping):
        return UNRECOGNIZED, (), "no 'game' object"
    if not game:
        return NO_DATA, (), "empty 'game' object (unknown app or no stats)"
    stats = game.get("availableGameStats")
    if stats is not None and not isinstance(stats, Mapping):
        return UNRECOGNIZED, (), "'availableGameStats' is not an object"
    achievements = (stats or {}).get("achievements")
    if achievements is None:
        # The game is known (has a name/version) and declares no achievements.
        return (EMPTY, (), "") if game.get("gameName") or game.get("gameVersion") else (NO_DATA, (), "no game name")
    if not isinstance(achievements, list):
        return UNRECOGNIZED, (), "'achievements' is not a list"
    items, seen = [], set()
    for a in achievements:
        name = a.get("name") if isinstance(a, Mapping) else None
        if not isinstance(name, str) or not name or name in seen:
            return UNRECOGNIZED, (), f"invalid or duplicate achievement name {name!r}"
        seen.add(name)
        display = a.get("displayName") if isinstance(a.get("displayName"), str) else ""
        hidden = a.get("hidden")
        items.append((name, display, None if hidden not in (0, 1) else bool(hidden)))
    return (OK if items else EMPTY), tuple(items), ""


def _percent(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        pct = float(value)
    elif isinstance(value, str):
        try:
            pct = float(value)
        except ValueError:
            return None
    else:
        return None
    return pct if math.isfinite(pct) and 0.0 <= pct <= 100.0 else None


def parse_percentages(body: Mapping[str, Any]) -> tuple[str, tuple, str]:
    """(status, ((api_name, pct), ...), detail) from GetGlobalAchievementPercentagesForApp."""
    wrapper = body.get("achievementpercentages")
    if not isinstance(wrapper, Mapping):
        return UNRECOGNIZED, (), "no 'achievementpercentages' object"
    achievements = wrapper.get("achievements")
    if achievements is None:
        return NO_DATA, (), "no 'achievements' list"
    if not isinstance(achievements, list):
        return UNRECOGNIZED, (), "'achievements' is not a list"
    items, seen = [], set()
    for a in achievements:
        name = a.get("name") if isinstance(a, Mapping) else None
        pct = _percent(a.get("percent")) if isinstance(a, Mapping) else None
        if not isinstance(name, str) or not name or name in seen:
            return UNRECOGNIZED, (), f"invalid or duplicate achievement name {name!r}"
        if pct is None:
            return UNRECOGNIZED, (), f"achievement {name!r} has invalid percent {a.get('percent')!r}"
        seen.add(name)
        items.append((name, pct))
    return (OK if items else EMPTY), tuple(items), ""


# --------------------------------------------------------------------------
# Features (pure)
# --------------------------------------------------------------------------


def fmt(x: float | None, places: int = 4) -> str:
    """Fixed-point, at most ``places`` decimals, no trailing zeros; '' for None."""
    if x is None:
        return ""
    text = f"{round(x, places):.{places}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _bool(value: bool | None) -> str:
    return "" if value is None else ("TRUE" if value else "FALSE")


@dataclass(frozen=True)
class Record:
    appid: int
    collected_at: str
    store: Outcome
    schema: Outcome
    pct: Outcome

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Record:
        raw = json.loads(text)
        out = {k: raw[k] for k in ("appid", "collected_at")}
        for k in ("store", "schema", "pct"):
            o = raw[k]
            o["items"] = tuple(tuple(i) for i in o["items"])
            out[k] = Outcome(**o)
        return cls(**out)

    @property
    def needs_retry(self) -> bool:
        return any(o.status in RETRY_STATUSES for o in (self.store, self.schema, self.pct))


def derive(record: Record) -> tuple[dict[str, str], list[dict[str, str]], dict[str, str]]:
    """(features row, per-achievement rows, sources row) for one game."""
    st, sc, pc = record.store, record.schema, record.pct
    positive = {
        "schema": sc.count if sc.status == OK else None,
        "global_percentages": pc.count if pc.status == OK else None,
        "store_summary": st.count if st.status == OK and st.count else None,
    }
    explicit_zero = {"schema": sc.status == EMPTY, "global_percentages": pc.status == EMPTY,
                     "store_summary": st.status == EMPTY}
    has_positive = any(v for v in positive.values()) or st.category is True
    has_zero = any(explicit_zero.values())
    conflict = has_positive and has_zero

    if has_positive:
        has = True
    elif has_zero:
        has = False
    else:
        has = None

    count, count_source = None, ""
    if has:
        for src in COUNT_SOURCES:
            if positive[src]:
                count, count_source = positive[src], src
                break
    elif has is False:
        for src in COUNT_SOURCES:
            if explicit_zero[src]:
                count, count_source = 0, src
                break

    pcts = [p for _, p in pc.items] if pc.status == OK else []
    available = True if pc.status == OK else (False if pc.status in (EMPTY, NOT_AVAILABLE) else None)
    snapshot = pc.retrieved_at or sc.retrieved_at or st.retrieved_at or record.collected_at
    features = {
        "appid": str(record.appid), "snapshot_utc": snapshot, "has_steam_achievements": _bool(has),
        "achievement_count": "" if count is None else str(count), "achievement_count_source": count_source,
        "global_unlock_percentages_available": _bool(available),
        "mean_global_unlock_pct": fmt(statistics.fmean(pcts)) if pcts else "",
        "median_global_unlock_pct": fmt(statistics.median(pcts)) if pcts else "",
        "min_global_unlock_pct": fmt(min(pcts)) if pcts else "",
        "share_below_10pct": fmt(sum(p < 10 for p in pcts) / len(pcts)) if pcts else "",
        "share_below_1pct": fmt(sum(p < 1 for p in pcts) / len(pcts)) if pcts else "",
    }

    # Per achievement: schema order first, then any names only in the percentages.
    pct_by_name = dict(pc.items) if pc.status == OK else {}
    schema_names = [n for n, _, _ in sc.items] if sc.status == OK else []
    rows = []
    for name, display, hidden in (sc.items if sc.status == OK else ()):
        rows.append(_ach_row(record.appid, snapshot, name, display, hidden, True, pct_by_name.get(name)))
    for name, pct in (pc.items if pc.status == OK else ()):
        if name not in schema_names:
            rows.append(_ach_row(record.appid, snapshot, name, "", None,
                                 False if sc.status == OK else None, pct))

    detail = "; ".join(d for d in (st.detail, sc.detail, pc.detail) if d)
    sources = {
        "appid": str(record.appid), "collected_at": record.collected_at,
        "store_status": st.status, "store_total": "" if st.count is None else str(st.count),
        "store_category": _bool(st.category), "store_url": st.url, "store_retrieved_at": st.retrieved_at,
        "schema_status": sc.status, "schema_count": "" if sc.count is None else str(sc.count), "schema_url": sc.url,
        "schema_retrieved_at": sc.retrieved_at, "pct_status": pc.status,
        "pct_count": "" if pc.count is None else str(pc.count), "pct_url": pc.url, "pct_retrieved_at": pc.retrieved_at,
        "conflict": _bool(conflict) if (has_positive or has_zero) else "", "detail": detail,
    }
    return features, rows, sources


def _ach_row(appid: int, snapshot: str, name: str, display: str, hidden: bool | None, in_schema: bool | None,
             pct: float | None) -> dict[str, str]:
    return {"appid": str(appid), "snapshot_utc": snapshot, "achievement_api_name": name, "display_name": display,
            "hidden": _bool(hidden), "in_schema": _bool(in_schema), "global_unlock_pct": fmt(pct),
            "pct_source": "GetGlobalAchievementPercentagesForApp" if pct is not None else ""}


# --------------------------------------------------------------------------
# State and collection
# --------------------------------------------------------------------------


class AchievementState:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS records (appid INTEGER PRIMARY KEY, record_json TEXT NOT NULL)")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> AchievementState:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def save(self, record: Record) -> None:
        with self._db:
            self._db.execute("INSERT INTO records VALUES (?, ?) ON CONFLICT(appid) DO UPDATE SET "
                             "record_json = excluded.record_json", (record.appid, record.to_json()))

    def records(self) -> dict[int, Record]:
        return {a: Record.from_json(j) for a, j in self._db.execute("SELECT appid, record_json FROM records ORDER BY appid")}


@dataclass
class Report:
    requested: int = 0
    attempted: int = 0
    pending: int = 0
    stop_reason: str = "complete"
    statuses: dict[str, dict[str, int]] = field(default_factory=dict)


class AchievementCollector:
    def __init__(self, state: AchievementState, web: SteamWebAPI, store: SteamStoreAPI, *,
                 now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
                 log: Callable[[str], None] = print):
        self.state = state
        self.web = web
        self.store = store
        self._now = now
        self._log = log

    def _store(self, appid: int, refresh: bool) -> Outcome:
        url = public_url(*appdetails_request(appid, self.store.country, self.store.language))
        try:
            fetched = self.store.fetch_app_details(appid, refresh=refresh)
        except (SteamRateLimitError, RequestBudgetExceeded):
            raise
        except SteamError as exc:
            return Outcome(ERROR, url, iso(self._now()), detail=f"appdetails: {type(exc).__name__}: {exc}")
        status, total, category, detail = parse_store(fetched.data)
        return Outcome(status, fetched.url, fetched.retrieved_at, count=total, category=category,
                       detail=f"appdetails: {detail}" if detail else "")

    def _web(self, name: str, request: tuple[str, dict], fetch, parse) -> Outcome:
        url = public_url(*request)
        try:
            body = fetch()
        except (SteamRateLimitError, RequestBudgetExceeded):
            raise
        except SteamHTTPError as exc:
            at = iso(self._now())
            if 400 <= exc.status < 500:
                return Outcome(NOT_AVAILABLE, url, at, detail=f"{name}: HTTP {exc.status}")
            return Outcome(ERROR, url, at, detail=f"{name}: HTTP {exc.status}")
        except SteamError as exc:
            return Outcome(ERROR, url, iso(self._now()), detail=f"{name}: {type(exc).__name__}: {exc}")
        status, items, detail = parse(body)
        return Outcome(status, url, iso(self._now()), count=len(items) if status in (OK, EMPTY) else None,
                       items=items, detail=f"{name}: {detail}" if detail else "")

    def collect_one(self, appid: int, *, refresh: bool = False) -> Record:
        store = self._store(appid, refresh)
        if self.web.has_key:
            schema_outcome = self._web("GetSchemaForGame", schema_for_game_request(appid),
                                       lambda: self.web.get_schema_for_game(appid), parse_schema)
        else:
            schema_outcome = Outcome(NOT_ATTEMPTED, public_url(*schema_for_game_request(appid)),
                                     detail="GetSchemaForGame: needs STEAM_API_KEY")
        pct = self._web("GetGlobalAchievementPercentagesForApp", global_achievement_percentages_request(appid),
                        lambda: self.web.get_global_achievement_percentages(appid), parse_percentages)
        return Record(appid, iso(self._now()), store, schema_outcome, pct)

    def run(self, appids: Iterable[int], *, limit: int | None = None, refresh: bool = False) -> Report:
        report = Report()
        wanted = list(dict.fromkeys(appids))
        report.requested = len(wanted)
        existing = self.state.records()
        # A run with a key upgrades records that could not try the schema.
        upgrade = self.web.has_key
        todo = [a for a in wanted if refresh or a not in existing or existing[a].needs_retry
                or (upgrade and existing[a].schema.status == NOT_ATTEMPTED)]
        self._log(f"{len(todo)} game(s) need achievement data")
        for appid in todo:
            if limit is not None and report.attempted >= limit:
                report.stop_reason = "limit_reached"
                break
            try:
                record = self.collect_one(appid, refresh=refresh)
            except SteamRateLimitError as exc:
                report.stop_reason = "rate_limited"
                self._log(f"stopping: {exc}")
                break
            except RequestBudgetExceeded:
                report.stop_reason = "budget_exhausted"
                break
            report.attempted += 1
            self.state.save(record)
        records = self.state.records()
        report.pending = sum(1 for a in wanted if a not in records or records[a].needs_retry)
        report.statuses = {k: dict(collections.Counter(getattr(records[a], k).status for a in wanted if a in records))
                           for k in ("store", "schema", "pct")}
        return report


# --------------------------------------------------------------------------
# Export and coverage
# --------------------------------------------------------------------------


def _csv(fields: Sequence[str], rows: Iterable[Mapping[str, str]]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def export(state: AchievementState, base: Path, work: Path) -> dict[str, Any]:
    """Write the features, per-achievement and source files for every collected game."""
    records = state.records()
    features, per_ach, sources = [], [], []
    for appid in sorted(records):
        f, rows, s = derive(records[appid])
        features.append(f)
        per_ach.extend(rows)
        sources.append(s)
    atomic_write_bytes(base / FEATURES_CSV, _csv(FEATURE_FIELDS, features))
    atomic_write_bytes(base / PER_ACHIEVEMENT_CSV, _csv(PER_ACHIEVEMENT_FIELDS, per_ach))
    atomic_write_bytes(work / "achievements" / "achievement_sources.csv", _csv(SOURCE_FIELDS, sources))
    return {"features": features, "per_achievement": per_ach, "sources": sources}


_STATUSES = (OK, EMPTY, NO_DATA, NOT_AVAILABLE, ERROR, UNRECOGNIZED, NOT_ATTEMPTED)


def coverage_markdown(exported: Mapping[str, Any], groups: Mapping[str, Sequence[int]], *, generated_at: str,
                      key_available: bool) -> str:
    """Coverage of the exported achievement data, per group of AppIDs (e.g. new vs original games)."""
    by_id = {int(f["appid"]): f for f in exported["features"]}
    src_by_id = {int(s["appid"]): s for s in exported["sources"]}
    w: list[str] = []
    w += ["# Achievement data coverage", "", f"Generated {generated_at} by `python -m crawler achievements`.", "",
          f"Steam Web API key: {'available' if key_available else 'not available, so GetSchemaForGame was skipped'}.",
          "",
          "> **Interpretation.** Global unlock percentages measure *observed rarity*: the share of Steam accounts",
          "> owning the game that unlocked an achievement. They reflect who bought and played the game and for how",
          "> long, and how many owners never launched it. They are **not** a measure of intrinsic difficulty.",
          ""]
    for name, ids in groups.items():
        ids = sorted(set(ids))
        feats = [by_id[a] for a in ids if a in by_id]
        srcs = [src_by_id[a] for a in ids if a in src_by_id]
        n = len(feats)
        w += [f"## {name}", "", f"Collected {n} of {len(ids)} games in this group "
              f"({len(ids) - n} not collected yet).", ""]
        if not n:
            continue
        cnt = lambda col: collections.Counter(f[col] or "blank" for f in feats)  # noqa: E731
        w += ["| Variable | TRUE | FALSE | blank (unknown) |", "|---|---|---|---|"]
        for col in ("has_steam_achievements", "global_unlock_percentages_available"):
            c = cnt(col)
            w.append(f"| `{col}` | {c.get('TRUE', 0)} | {c.get('FALSE', 0)} | {c.get('blank', 0)} |")
        w += ["", "| `achievement_count_source` | Games |", "|---|---|"]
        for src, k in sorted(cnt("achievement_count_source").items()):
            w.append(f"| {src} | {k} |")
        stats = [f for f in feats if f["mean_global_unlock_pct"]]
        w += ["", f"Unlock-percentage statistics are present for **{len(stats)}/{n}** games."]
        if stats:
            means = [float(f["mean_global_unlock_pct"]) for f in stats]
            counts = [int(f["achievement_count"]) for f in stats if f["achievement_count"]]
            rare = [float(f["share_below_1pct"]) for f in stats]
            w.append(f"Across them, the median per-game mean unlock rate is {fmt(statistics.median(means), 2)}%. "
                     f"The median achievement count is {statistics.median(counts) if counts else 'n/a'}, and the "
                     f"median share of achievements below 1% is {fmt(statistics.median(rare), 3)}.")
        w += ["", "| Source | " + " | ".join(_STATUSES) + " |", "|---|" + "---|" * len(_STATUSES)]
        for label, key in (("Store summary (appdetails)", "store_status"), ("GetSchemaForGame", "schema_status"),
                           ("GetGlobalAchievementPercentagesForApp", "pct_status")):
            c = collections.Counter(x[key] for x in srcs)
            w.append(f"| {label} | " + " | ".join(str(c.get(k, 0)) for k in _STATUSES) + " |")
        conflicts = [x["appid"] for x in srcs if x["conflict"] == "TRUE"]
        both = [x for x in srcs if x["store_total"] and x["pct_count"]]
        agree = sum(x["store_total"] == x["pct_count"] for x in both)
        w += ["", "Conflicting evidence (one source shows achievements, another explicitly none): "
              + (", ".join(conflicts) if conflicts else "none") + ".",
              f"Store summary total vs global-percentages count: {agree}/{len(both)} games with both sources agree.", ""]
    w += [f"Per-achievement rows in total: {len(exported['per_achievement'])}.", "",
          "## Status meanings", "",
          f"* `{OK}`: the source lists achievements.",
          f"* `{EMPTY}`: the source *explicitly* reports zero.",
          f"* `{NO_DATA}`: the source answered without saying either way.",
          f"* `{NOT_AVAILABLE}`: the source refused the app (HTTP 4xx or success=false). This is never read as zero.",
          f"* `{ERROR}`: the request failed; it is retried on the next run.",
          f"* `{UNRECOGNIZED}`: the response had an unexpected shape.",
          f"* `{NOT_ATTEMPTED}`: the source needs a key, and none was set.", ""]
    return "\n".join(w) + "\n"

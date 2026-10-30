"""Fetch and normalize Store metadata for eligible AppIDs.

For each AppID: Store ``appdetails`` (core fields) and
``IStoreBrowseService/GetItems`` (Early Access flag, and user tag ids with
names from one ``IStoreService/GetTagList`` call per invocation), or, with
``tags_source="steamspy"``, tags from SteamSpy (cached). ``sentiment`` is filled at
export time from verified GetAppReviews records (crawler.reviews), never
fetched here. Results are checkpointed per app in SQLite; exports are
regenerated from that state:

* ``games.csv``       legacy 13-column format, validated by crawler.schema
* ``provenance.csv``  one row per (appid, field): raw value, source URL, time, status
* ``prices.csv``      the price provenance flattened: currency, regular/sale cents, discount
* ``tag_votes.csv``   the top-20 tags with vote counts (SteamSpy) or weights (Steam)
* ``early_access.csv`` TRUE / FALSE / unknown with the evidence behind it
* ``failed.csv``      AppIDs whose row could not be built, with the reason
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from crawler import schema
from crawler.discovery import write_csv_atomic
from crawler.discovery_state import iso
from crawler.metadata import (
    PRICE_CONVERSION,
    PROVENANCE_FIELDS,
    TAGS_STEAM,
    TAGS_STEAMSPY,
    ExtractionError,
    FieldProvenance,
    SourceResult,
    normalize,
    with_review,
)
from crawler.reviews import ReviewRecord
from crawler.steamspy import SteamSpyClient
from crawler.steamspy import appdetails_request as spy_appdetails_request
from crawler.steam_api import (
    SteamStoreAPI,
    SteamWebAPI,
    appdetails_request,
    public_url,
    store_item_tag_weights,
    store_items_request,
    tag_list_request,
)
from crawler.transport import RequestBudgetExceeded, SteamError, SteamRateLimitError

FAILED_FIELDS = ("appid", "error", "detail", "attempts", "last_attempt_at")
TAG_VOTE_FIELDS = ("appid", "rank", "tag", "count", "count_kind", "source", "source_url", "retrieved_at")
EARLY_ACCESS_FIELDS = ("appid", "early_access", "status", "reason", "store_item_is_early_access",
                       "genres_early_access", "store_item_url", "store_item_retrieved_at", "appdetails_url",
                       "appdetails_retrieved_at")
PRICE_FIELDS = ("appid", "price", "status", "currency", "initial_cents", "final_cents", "discount_percent",
                "is_free", "source", "source_url", "retrieved_at", "conversion")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS extracted (
    appid           INTEGER PRIMARY KEY,
    row_json        TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    complete        INTEGER NOT NULL,
    extracted_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS failures (
    appid           INTEGER PRIMARY KEY,
    error           TEXT NOT NULL,
    detail          TEXT NOT NULL,
    attempts        INTEGER NOT NULL,
    last_attempt_at TEXT NOT NULL
);
"""


def read_eligible_appids(path: str | Path) -> list[int]:
    """AppIDs from discovery's eligible.csv, in file order, without duplicates."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "appid" not in reader.fieldnames:
            raise ValueError(f"{path} has no 'appid' column")
        return list(dict.fromkeys(int(row["appid"]) for row in reader))


class ExtractionState:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._db:
            self._db.executescript(_SCHEMA)
            columns = {r[1] for r in self._db.execute("PRAGMA table_info(extracted)")}
            if "extras_json" not in columns:  # state files created before extras existed
                self._db.execute("ALTER TABLE extracted ADD COLUMN extras_json TEXT NOT NULL DEFAULT '{}'")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> ExtractionState:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def status(self) -> tuple[set[int], set[int], set[int]]:
        """(complete, partial, failed) AppIDs."""
        complete, partial = set(), set()
        for appid, done in self._db.execute("SELECT appid, complete FROM extracted"):
            (complete if done else partial).add(appid)
        failed = {r[0] for r in self._db.execute("SELECT appid FROM failures")}
        return complete, partial, failed

    def save(self, row: dict[str, str], provenance: Sequence[FieldProvenance], complete: bool, now: dt.datetime,
             extras: Mapping | None = None) -> None:
        appid = int(row["id"])
        with self._db:
            self._db.execute(
                """INSERT INTO extracted (appid, row_json, provenance_json, complete, extracted_at, extras_json)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(appid) DO UPDATE SET row_json = excluded.row_json,
                       provenance_json = excluded.provenance_json, complete = excluded.complete,
                       extracted_at = excluded.extracted_at, extras_json = excluded.extras_json""",
                (appid, json.dumps(row, ensure_ascii=False), json.dumps([p.as_row() for p in provenance], ensure_ascii=False),
                 int(complete), iso(now), json.dumps(extras or {}, ensure_ascii=False)),
            )
            self._db.execute("DELETE FROM failures WHERE appid = ?", (appid,))

    def fail(self, appid: int, error: str, detail: str, now: dt.datetime) -> None:
        with self._db:
            self._db.execute(
                """INSERT INTO failures VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(appid) DO UPDATE SET error = excluded.error, detail = excluded.detail,
                       attempts = failures.attempts + 1, last_attempt_at = excluded.last_attempt_at""",
                (appid, error, detail, iso(now)),
            )

    def rows(self) -> list[tuple[dict[str, str], list[dict[str, str]]]]:
        return [(json.loads(r), json.loads(p))
                for r, p in self._db.execute("SELECT row_json, provenance_json FROM extracted ORDER BY appid")]

    def extras(self) -> dict[int, dict]:
        return {a: json.loads(e) for a, e in self._db.execute("SELECT appid, extras_json FROM extracted ORDER BY appid")}

    def failures(self) -> list[dict[str, str]]:
        cur = self._db.execute(f"SELECT {', '.join(FAILED_FIELDS)} FROM failures ORDER BY appid")
        return [dict(zip(FAILED_FIELDS, map(str, r))) for r in cur]


@dataclass
class ExtractReport:
    requested: int = 0
    skipped_legacy: list[int] = field(default_factory=list)
    attempted: int = 0
    extracted: int = 0
    partial: int = 0
    failed: int = 0
    pending: int = 0
    stop_reason: str = "complete"


class Extractor:
    def __init__(
        self,
        state: ExtractionState,
        *,
        web: SteamWebAPI,
        store: SteamStoreAPI,
        legacy_ids: frozenset[int],
        tags_source: str = TAGS_STEAM,
        steamspy: SteamSpyClient | None = None,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
        log: Callable[[str], None] = print,
    ):
        if tags_source not in (TAGS_STEAM, TAGS_STEAMSPY):
            raise ValueError(f"unknown tags_source {tags_source!r}")
        if tags_source == TAGS_STEAMSPY and steamspy is None:
            raise ValueError("tags_source='steamspy' needs a SteamSpyClient")
        self.tags_source = tags_source
        self.steamspy = steamspy
        self.state = state
        self.web = web
        self.store = store
        self.legacy_ids = legacy_ids
        self._now = now
        self._log = log
        self._refresh = False
        self._tag_names: dict[int, str] | None = None
        self._tag_names_error: str | None = None

    def _source(self, name: str, request: tuple[str, dict], fetch: Callable[[], object]) -> SourceResult:
        url = public_url(*request)
        try:
            data = fetch()
        except (SteamRateLimitError, RequestBudgetExceeded):
            raise
        except SteamError as exc:
            return SourceResult(name, url, iso(self._now()), error=f"{type(exc).__name__}: {exc}")
        return SourceResult(name, url, iso(self._now()), data=data)

    def _load_tag_names(self) -> dict[int, str] | None:
        if self._tag_names is None and self._tag_names_error is None:
            result = self._source("tag_list", tag_list_request(self.store.language),
                                  lambda: self.web.get_tag_names(language=self.store.language))
            if result.ok:
                self._tag_names = result.data
            else:
                self._tag_names_error = result.error
                self._log(f"tag names unavailable: {result.error}")
        return self._tag_names

    def extract_one(self, appid: int) -> SourceResult | None:
        """Fetch all sources for one app and store the result. Returns the failed appdetails source, if any."""
        cc, lang = self.store.country, self.store.language
        details = self._details(appid)
        if not details.ok:
            self.state.fail(appid, "appdetails_error", details.error or "", self._now())
            return details
        if details.data is None:
            self.state.fail(appid, "store_details_unavailable", "appdetails returned success=false", self._now())
            return None
        item = self._source("store_items", store_items_request(appid, cc, lang),
                            lambda: self.web.get_store_item(appid, country=cc, language=lang))
        if self.tags_source == TAGS_STEAMSPY:
            tags, tag_names = self._steamspy_source(appid), None
        else:
            tags, tag_names = self._steam_tags(item), self._load_tag_names()
        try:
            extracted = normalize(appid, details, tags, tag_names, store_item=item)
        except ExtractionError as exc:
            self.state.fail(appid, "normalization_error", str(exc), self._now())
            return None
        self.state.save(extracted.row, extracted.provenance, extracted.complete, self._now(), extracted.extras)
        return None

    def _details(self, appid: int) -> SourceResult:
        """appdetails via the Store client's response cache (bypassed with refresh)."""
        url = public_url(*appdetails_request(appid, self.store.country, self.store.language))
        try:
            fetched = self.store.fetch_app_details(appid, refresh=self._refresh)
        except (SteamRateLimitError, RequestBudgetExceeded):
            raise
        except SteamError as exc:
            return SourceResult("appdetails", url, iso(self._now()), error=f"{type(exc).__name__}: {exc}")
        return SourceResult("appdetails", fetched.url, fetched.retrieved_at, data=fetched.data,
                            cached=fetched.from_cache)

    @staticmethod
    def _steam_tags(item: SourceResult) -> SourceResult:
        """Tag ids from the already-fetched GetItems store item (no extra request)."""
        if not item.ok or item.data is None:
            return item
        try:
            tagids = [t for t, _ in store_item_tag_weights(item.data)]
        except SteamError as exc:
            return SourceResult(item.name, item.url, item.retrieved_at, error=f"{type(exc).__name__}: {exc}")
        return SourceResult(item.name, item.url, item.retrieved_at, data=tagids)

    def _steamspy_source(self, appid: int) -> SourceResult:
        url = public_url(*spy_appdetails_request(appid))
        try:
            response = self.steamspy.appdetails(appid)
        except (SteamRateLimitError, RequestBudgetExceeded):
            raise
        except SteamError as exc:
            return SourceResult(TAGS_STEAMSPY, url, iso(self._now()), error=f"{type(exc).__name__}: {exc}")
        return SourceResult(TAGS_STEAMSPY, response.url, response.retrieved_at, data=response.data,
                            cached=response.from_cache)

    def run(self, appids: Iterable[int], *, limit: int | None = None, refresh: bool = False) -> ExtractReport:
        report = ExtractReport()
        self._refresh = refresh
        wanted = list(dict.fromkeys(appids))
        report.requested = len(wanted)
        report.skipped_legacy = [a for a in wanted if a in self.legacy_ids]
        if report.skipped_legacy:
            self._log(f"skipping {len(report.skipped_legacy)} AppID(s) already in the legacy CSV")
        complete, partial, failed = self.state.status()
        candidates = [a for a in wanted if a not in self.legacy_ids]
        # A row extracted with another tag source is stale for this run.
        stored_sources = {a: e.get("tags_source", TAGS_STEAM) for a, e in self.state.extras().items()}
        stale = {a for a in complete if stored_sources.get(a, TAGS_STEAM) != self.tags_source}
        if refresh:
            todo = candidates
        else:
            # Never-attempted apps first, then retries of partial/failed/stale ones.
            fresh = [a for a in candidates if a not in complete | partial | failed]
            todo = fresh + [a for a in candidates if a in partial | failed | stale]
        self._log(f"{len(todo)} AppID(s) to extract")

        for appid in todo:
            if limit is not None and report.attempted >= limit:
                report.stop_reason = "limit_reached"
                break
            report.attempted += 1
            try:
                self.extract_one(appid)
            except SteamRateLimitError as exc:
                report.attempted -= 1
                report.stop_reason = "rate_limited"
                self._log(f"stopping: {exc}")
                break
            except RequestBudgetExceeded:
                report.attempted -= 1
                report.stop_reason = "budget_exhausted"
                break

        complete_after, partial_after, failed_after = self.state.status()
        report.extracted = len(complete_after & set(candidates))
        report.partial = len(partial_after & set(candidates))
        report.failed = len(failed_after & set(candidates))
        report.pending = len([a for a in candidates if a not in complete_after | partial_after | failed_after])
        return report


def price_row(price: Mapping[str, str]) -> dict[str, str]:
    """Flatten a price provenance record into explicit original-amount columns."""
    raw = json.loads(price["raw"]) if price["raw"] else {}
    po = raw.get("price_overview") if isinstance(raw.get("price_overview"), dict) else {}

    def text(value: object) -> str:
        return "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)

    return {
        "appid": price["appid"], "price": price["value"], "status": price["status"],
        "currency": text(po.get("currency")), "initial_cents": text(po.get("initial")),
        "final_cents": text(po.get("final")), "discount_percent": text(po.get("discount_percent")),
        "is_free": text(raw.get("is_free")), "source": price["source"], "source_url": price["source_url"],
        "retrieved_at": price["retrieved_at"], "conversion": PRICE_CONVERSION,
    }


def export(state: ExtractionState, out_dir: str | Path,
           reviews: Mapping[int, ReviewRecord] | None = None) -> dict[str, int]:
    """Write games.csv (legacy format), provenance.csv, prices.csv and failed.csv.

    ``sentiment`` comes only from ``reviews`` (verified GetAppReviews records).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    reviews = reviews or {}
    stored = [with_review(row, prov, reviews.get(int(row["id"]))) for row, prov in state.rows()]
    games_tmp = out / ".games.csv.tmp"
    try:
        schema.write_rows(games_tmp, [row for row, _ in stored], overwrite=True)  # validates every row
        os.replace(games_tmp, out / "games.csv")
    except BaseException:
        games_tmp.unlink(missing_ok=True)
        raise
    write_csv_atomic(out / "provenance.csv", PROVENANCE_FIELDS, (p for _, prov in stored for p in prov))
    write_csv_atomic(out / "prices.csv", PRICE_FIELDS,
                     (price_row(p) for _, prov in stored for p in prov if p["field"] == "price"))
    extras = state.extras()
    write_csv_atomic(out / "tag_votes.csv", TAG_VOTE_FIELDS,
                     ({"appid": a, **v} for a, e in extras.items() for v in e.get("tag_votes", [])))
    write_csv_atomic(out / "early_access.csv", EARLY_ACCESS_FIELDS,
                     ({"appid": a, **e["early_access"]} for a, e in extras.items() if "early_access" in e))
    failures = state.failures()
    write_csv_atomic(out / "failed.csv", FAILED_FIELDS, failures)
    return {"games": len(stored), "provenance_rows": sum(len(p) for _, p in stored), "failed": len(failures)}

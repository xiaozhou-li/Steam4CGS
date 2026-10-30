"""Discover new Steam games released inside a date window.

Phase 1 pages through ``IStoreService/GetAppList/v1`` (games only) until
``have_more_results`` is false, checkpointing after every page. Phase 2 looks
up each non-legacy candidate with Store ``appdetails`` and stores the facts.
Decisions are recomputed from stored facts at export time, so they always
reflect the run's window. See docs/discovery.md.
"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from crawler import schema
from crawler.discovery_state import DiscoveryState, Run, iso
from crawler.eligibility import (
    ELIGIBLE,
    EXCLUDED,
    FAILED,
    AppFacts,
    DateWindow,
    Decision,
    classify,
    failed_decision,
    legacy_decision,
)
from crawler.steam_api import SteamStoreAPI, SteamWebAPI
from crawler.transport import RequestBudgetExceeded, SteamError, SteamRateLimitError, SteamResponseError

DEFAULT_START = dt.date(2020, 1, 1)
DEFAULT_PAGE_SIZE = 10_000

# Stop reasons
COMPLETE = "complete"
LIMIT_REACHED = "limit_reached"
RATE_LIMITED = "rate_limited"
TOO_MANY_FAILURES = "too_many_failures"
BUDGET_EXHAUSTED = "budget_exhausted"

PENDING = "pending"

DISCOVERED_FIELDS = ("appid", "name", "status", "reason", "last_modified", "price_change_number",
                     "first_seen_at", "last_seen_at")
ELIGIBLE_FIELDS = ("appid", "name", "type", "release_date", "release_date_precision", "release_date_earliest",
                   "release_date_latest", "reason", "detail", "fetched_at")
EXCLUDED_FIELDS = ("appid", "name", "reason", "detail", "type", "release_date", "fetched_at")
FAILED_FIELDS = ("appid", "name", "reason", "error", "detail", "attempts", "last_attempt_at")


class DiscoveryConfigError(Exception):
    """The requested run conflicts with the saved state (e.g. an unfinished run exists)."""


def load_legacy_appids(path: str | Path) -> frozenset[int]:
    """AppIDs of the original CSV, read without modifying it."""
    ids = [int(row["id"]) for row in schema.read_rows(path)]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path} contains duplicate ids")
    return frozenset(ids)


@dataclass(frozen=True)
class DiscoveryOptions:
    window: DateWindow | None = None   # None: keep the resumed run's window, else default
    limit: int | None = None           # max appdetails lookups in this invocation
    incremental: bool = False
    resume: bool = False
    restart: bool = False
    recheck_unreleased: bool = False
    page_size: int = DEFAULT_PAGE_SIZE
    max_consecutive_failures: int = 5
    # Seed mode: evaluate exactly these AppIDs instead of paging GetAppList (no key needed).
    seed_appids: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        if self.seed_appids is not None and self.incremental:
            raise ValueError("seed AppIDs and --incremental are mutually exclusive")
        if self.seed_appids is not None and any(a <= 0 for a in self.seed_appids):
            raise ValueError("seed AppIDs must be positive")
        if self.limit is not None and self.limit < 0:
            raise ValueError("limit must be >= 0")
        if self.resume and self.restart:
            raise ValueError("--resume and --restart are mutually exclusive")


@dataclass
class RunReport:
    run_id: int
    window: DateWindow
    mode: str
    stop_reason: str
    catalog_pages_this_invocation: int = 0
    new_apps: int = 0
    lookups: int = 0
    lookup_failures: int = 0
    pending: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def finished(self) -> bool:
        return self.stop_reason == COMPLETE


class Discovery:
    def __init__(
        self,
        state: DiscoveryState,
        *,
        web_factory: Callable[[], SteamWebAPI],
        store: SteamStoreAPI,
        legacy_ids: frozenset[int],
        today: Callable[[], dt.date] = dt.date.today,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
        log: Callable[[str], None] = print,
    ):
        self.state = state
        self._web_factory = web_factory
        self._web: SteamWebAPI | None = None
        self.store = store
        self.legacy_ids = legacy_ids
        self._today = today
        self._now = now
        self._log = log

    # -- run lifecycle -------------------------------------------------------------

    def _open_run(self, opts: DiscoveryOptions) -> Run:
        run = self.state.unfinished_run()
        if run is not None and opts.restart:
            self.state.set_run_status(run.run_id, "abandoned", self._now())
            self._log(f"abandoned unfinished run #{run.run_id}")
            run = None
        if opts.resume:
            if run is None:
                raise DiscoveryConfigError("there is no unfinished run to resume")
            if opts.window is not None and opts.window != run.window:
                raise DiscoveryConfigError(
                    f"run #{run.run_id} uses window {run.window}; it cannot be resumed with {opts.window}")
            return run
        if run is not None:
            raise DiscoveryConfigError(
                f"run #{run.run_id} (window {run.window}, started {run.started_at}) is unfinished; "
                "pass --resume to continue it or --restart to abandon it")

        window = opts.window or DateWindow(DEFAULT_START, self._today())
        if opts.seed_appids is not None:
            return self.state.start_run(window=window, mode="seed", if_modified_since=None,
                                        recheck_unreleased=opts.recheck_unreleased, now=self._now())
        # A new catalog run always starts with GetAppList, so fail on a missing key before recording it.
        self._web = self._web or self._web_factory()
        if_modified_since = None
        if opts.incremental:
            previous = self.state.last_finished_run()
            if previous is None:
                raise DiscoveryConfigError("incremental discovery needs a previously finished run; run a full discovery first")
            if_modified_since = previous.catalog_started_at
        return self.state.start_run(
            window=window, mode="incremental" if opts.incremental else "full",
            if_modified_since=if_modified_since, recheck_unreleased=opts.recheck_unreleased, now=self._now(),
        )

    def run(self, opts: DiscoveryOptions = DiscoveryOptions()) -> RunReport:
        run = self._open_run(opts)
        report = RunReport(run.run_id, run.window, run.mode, stop_reason=COMPLETE)
        self._log(f"run #{run.run_id}: mode={run.mode} window={run.window}"
                  + (f" if_modified_since={run.if_modified_since}" if run.if_modified_since is not None else ""))
        if not run.catalog_complete:
            if run.mode == "seed":
                if opts.seed_appids is None:
                    raise DiscoveryConfigError(f"run #{run.run_id} is a seed run; resume it with the same AppIDs")
                self.state.save_seeds(run.run_id, dict.fromkeys(opts.seed_appids), self._now())
                self._log(f"seed mode: {len(set(opts.seed_appids))} AppID(s), GetAppList not used")
            else:
                self._crawl_catalog(run, opts, report)
            run = self.state.get_run(run.run_id)
        self._evaluate(run, opts, report)

        report.new_apps = self.state.count_first_seen(run.run_id)
        report.pending = len(self._pending(run, opts))
        if report.stop_reason == COMPLETE and report.pending:
            report.stop_reason = LIMIT_REACHED  # safety net: never finish a run with work left
        if report.stop_reason == COMPLETE:
            self.state.set_run_status(run.run_id, "finished", self._now())
        return report

    # -- phase 1: catalog -------------------------------------------------------------

    def _crawl_catalog(self, run: Run, opts: DiscoveryOptions, report: RunReport) -> None:
        if self._web is None:
            self._web = self._web_factory()
        cursor = run.catalog_cursor
        if cursor is not None:
            self._log(f"resuming catalog after appid {cursor} ({run.catalog_pages} page(s) already saved)")
        while True:
            page = self._web.get_app_list(
                last_appid=cursor, max_results=opts.page_size, if_modified_since=run.if_modified_since)
            more = page.have_more_results
            if more and (page.last_appid is None or (cursor is not None and page.last_appid <= cursor)):
                raise SteamResponseError("GetAppList pagination did not advance last_appid")
            next_cursor = page.last_appid if more else cursor
            self.state.save_catalog_page(run.run_id, page.apps, cursor=next_cursor, complete=not more, now=self._now())
            report.catalog_pages_this_invocation += 1
            self._log(f"catalog page {run.catalog_pages + report.catalog_pages_this_invocation}: "
                      f"{len(page.apps)} app(s), have_more_results={more}")
            if not more:
                return
            cursor = next_cursor

    # -- phase 2: metadata -------------------------------------------------------------

    def _pending(self, run: Run, opts: DiscoveryOptions) -> list[tuple[int, int | None]]:
        pending = self.state.pending(run, self.legacy_ids)
        if run.mode == "seed" and opts.seed_appids is not None:
            seeds = set(opts.seed_appids)
            pending = [p for p in pending if p[0] in seeds]
        return pending

    def _evaluate(self, run: Run, opts: DiscoveryOptions, report: RunReport) -> None:
        pending = self._pending(run, opts)
        self._log(f"{len(pending)} app(s) need appdetails")
        consecutive_failures = 0
        for appid, last_modified in pending:
            if opts.limit is not None and report.lookups >= opts.limit:
                report.stop_reason = LIMIT_REACHED
                return
            report.lookups += 1
            try:
                data = self.store.get_app_details(appid)
            except SteamRateLimitError as exc:
                # Not the app's fault: leave it pending and stop so the run can be resumed later.
                report.lookups -= 1
                report.stop_reason = RATE_LIMITED
                self._log(f"stopping: {exc}")
                return
            except RequestBudgetExceeded:
                report.lookups -= 1
                report.stop_reason = BUDGET_EXHAUSTED
                return
            except SteamError as exc:
                self.state.record_failure(appid, error=type(exc).__name__, detail=str(exc),
                                          run_id=run.run_id, now=self._now())
                report.lookup_failures += 1
                consecutive_failures += 1
                if consecutive_failures >= opts.max_consecutive_failures:
                    report.stop_reason = TOO_MANY_FAILURES
                    self._log(f"stopping after {consecutive_failures} consecutive failures: {exc}")
                    return
                continue
            consecutive_failures = 0
            self.state.record_facts(AppFacts.from_appdetails(appid, data), source_last_modified=last_modified,
                                    run_id=run.run_id, now=self._now())
            if report.lookups % 100 == 0:
                self._log(f"{report.lookups} lookup(s) done")


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def decide_all(state: DiscoveryState, window: DateWindow, legacy_ids: frozenset[int]) -> Iterable[tuple]:
    """Yield ``(app, decision | None, stored_facts | None, failure | None)`` for every discovered app."""
    facts = state.all_facts()
    failures = state.all_failures()
    for app in state.iter_discovered():
        stored, failure = facts.get(app.appid), failures.get(app.appid)
        if app.appid in legacy_ids:
            decision = legacy_decision(app.appid)
        elif failure is not None:
            decision = failed_decision(app.appid, failure.error, failure.detail)
        elif stored is not None:
            decision = classify(stored.facts, window, legacy_ids)
        else:
            decision = None
        yield app, decision, stored, failure


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a sibling ``.<name>.tmp``, fsync, then rename; the temp file never outlives a failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_csv_atomic(path: Path, fields: Sequence[str], rows: Iterable[dict]) -> None:
    """Write a CRLF CSV via a sibling temp file and an atomic rename."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\r\n")
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())  # the rename must never expose a partially written file
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def export(state: DiscoveryState, out_dir: str | Path, run: Run, legacy_ids: frozenset[int],
           report: RunReport | None = None, *, now: dt.datetime | None = None) -> dict[str, int]:
    """Write discovered/eligible/excluded/failed CSVs and summary.json; return status counts."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    discovered, eligible, excluded, failed = [], [], [], []
    counts: collections.Counter[str] = collections.Counter()
    reasons: collections.Counter[str] = collections.Counter()

    for app, decision, stored, failure in decide_all(state, run.window, legacy_ids):
        status = decision.status if decision else PENDING
        counts[status] += 1
        if decision:
            reasons[decision.reason] += 1
        discovered.append({
            "appid": app.appid, "name": app.name, "status": status, "reason": decision.reason if decision else "",
            "last_modified": app.last_modified, "price_change_number": app.price_change_number,
            "first_seen_at": app.first_seen_at, "last_seen_at": app.last_seen_at,
        })
        facts: AppFacts | None = stored.facts if stored else None
        name = (facts.name if facts and facts.name else None) or app.name
        if decision is None:
            continue
        if decision.status == ELIGIBLE:
            rel = decision.release
            eligible.append({
                "appid": app.appid, "name": name, "type": facts.type, "release_date": facts.release_date_raw,
                "release_date_precision": rel.precision, "release_date_earliest": rel.earliest.isoformat(),
                "release_date_latest": rel.latest.isoformat(), "reason": decision.reason,
                "detail": decision.detail, "fetched_at": stored.fetched_at,
            })
        elif decision.status == EXCLUDED:
            excluded.append({
                "appid": app.appid, "name": name, "reason": decision.reason, "detail": decision.detail,
                "type": facts.type if facts else "", "release_date": facts.release_date_raw if facts else "",
                "fetched_at": stored.fetched_at if stored else "",
            })
        elif decision.status == FAILED:
            failed.append({
                "appid": app.appid, "name": name, "reason": decision.reason, "error": failure.error,
                "detail": failure.detail, "attempts": failure.attempts, "last_attempt_at": failure.last_attempt_at,
            })

    write_csv_atomic(out / "discovered.csv", DISCOVERED_FIELDS, discovered)
    write_csv_atomic(out / "eligible.csv", ELIGIBLE_FIELDS, eligible)
    write_csv_atomic(out / "excluded.csv", EXCLUDED_FIELDS, excluded)
    write_csv_atomic(out / "failed.csv", FAILED_FIELDS, failed)

    summary = {
        "generated_at": iso(now or dt.datetime.now(dt.timezone.utc)),
        "run_id": run.run_id,
        "run_status": state.get_run(run.run_id).status,
        "mode": run.mode,
        "window": {"start": run.window.start.isoformat(), "end": run.window.end.isoformat()},
        "stop_reason": report.stop_reason if report else None,
        "counts": {"discovered": len(discovered), **{s: counts[s] for s in (ELIGIBLE, EXCLUDED, FAILED, PENDING)}},
        "reasons": dict(sorted(reasons.items())),
    }
    atomic_write_bytes(out / "summary.json", (json.dumps(summary, indent=2) + "\n").encode("utf-8"))
    if report is not None:
        report.counts, report.reasons = summary["counts"], summary["reasons"]
    return summary["counts"]

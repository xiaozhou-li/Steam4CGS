"""The integrated, resumable crawler: workspace, stages and final outputs.

Stages (each one persists its own checkpoints in ``work/``):

* ``audit``     read-only checks of the legacy CSV, credentials and workspace
* ``discover``  catalog (or seed AppIDs) -> eligible games (crawler.discovery)
* ``crawl``     metadata + review summaries for eligible games (crawler.extract, crawler.reviews)
* ``export``    the final outputs, written atomically
* ``validate``  re-checks every output against the contract
* ``run``       discover -> crawl -> export -> validate

Final outputs (``--base-dir``, default the project root):

* ``steamproducts2020_2026_new.csv``     new games only, the 13 legacy columns
* ``steamproducts2020_2026_merged.csv``  the legacy file byte-for-byte, then the new rows
* ``steam_metadata_provenance.csv``      one row per (new AppID, column)
* ``crawl_manifest.json``                window, sources, counts, hashes, limitations
* ``work/failed_appids.csv``             every AppID that failed at any stage
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
import platform
import sqlite3
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from crawler import discovery, extract, preflight, schema
from crawler import reviews as rv
from crawler import steamspy as spy
from crawler.cache import ResponseCache
from crawler.credentials import InvalidCredentialError, MissingCredentialError, load_api_key
from crawler.discovery_state import DiscoveryState, iso
from crawler.eligibility import ELIGIBLE, FAILED
from crawler.extract import ExtractionState
from crawler.metadata import PROVENANCE_FIELDS, TAGS_STEAM, TAGS_STEAMSPY, with_review
from crawler.steam_api import DEFAULT_APPDETAILS_TTL, SteamStoreAPI, SteamWebAPI
from crawler.transport import SteamAuthError, SteamError, SteamTransport

ROOT = Path(__file__).resolve().parent.parent
LEGACY_SHA256 = "3a7b7bd5b59565440d05c543557607b498c45590ebddc3f3e7a77afe9d095be8"
LEGACY_ROWS = 23034

NEW_CSV = "steamproducts2020_2026_new.csv"
MERGED_CSV = "steamproducts2020_2026_merged.csv"
PROVENANCE_CSV = "steam_metadata_provenance.csv"
MANIFEST_JSON = "crawl_manifest.json"
FAILED_CSV = "failed_appids.csv"
FAILED_FIELDS = ("appid", "stage", "error", "detail", "attempts", "last_attempt_at")

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_TEMPFAIL, EXIT_INTERRUPTED = 0, 1, 2, 75, 130

LIMITATIONS = (
    "Store appdetails and appreviews-style endpoints are undocumented and may change without notice.",
    "Prices are whole USD from the regular listing price (cc=us); currency and amounts are in provenance.",
    "Release dates are kept verbatim when the store gives less than a full date; no day is invented.",
    "early_access is blank (unknown) unless the Store states it explicitly; absence is never FALSE.",
    "sentiment is filled only from verified IUserReviewsService/GetAppReviews summaries.",
    "tags are blank when the selected source has none; genres are never substituted.",
    "publisher never carries the legacy 'Franchise:' suffix (not exposed by appdetails).",
    "SteamSpy (optional) is a third-party estimate service and can lag the store.",
)


# --------------------------------------------------------------------------
# Workspace, lock, status, recovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Workspace:
    base: Path
    work: Path
    legacy_csv: Path

    @classmethod
    def at(cls, base: Path | None = None, work: Path | None = None, legacy_csv: Path | None = None) -> Workspace:
        base = Path(base or ROOT)
        return cls(base, Path(work or base / "work"), Path(legacy_csv or ROOT / "steamproducts2020.csv"))

    # state
    discovery_state = property(lambda self: self.work / "discovery.sqlite3")
    metadata_state = property(lambda self: self.work / "metadata.sqlite3")
    reviews_state = property(lambda self: self.work / "reviews.sqlite3")
    appdetails_cache = property(lambda self: self.work / "cache" / "appdetails.sqlite3")
    steamspy_cache = property(lambda self: self.work / "cache" / "steamspy.sqlite3")
    status_json = property(lambda self: self.work / "crawl_status.json")
    lock_file = property(lambda self: self.work / "crawl.lock")
    # intermediate exports
    discovery_out = property(lambda self: self.work / "discovery")
    metadata_out = property(lambda self: self.work / "metadata")
    reviews_out = property(lambda self: self.work / "reviews")
    failed_csv = property(lambda self: self.work / FAILED_CSV)
    # final outputs
    new_csv = property(lambda self: self.base / NEW_CSV)
    merged_csv = property(lambda self: self.base / MERGED_CSV)
    provenance_csv = property(lambda self: self.base / PROVENANCE_CSV)
    manifest = property(lambda self: self.base / MANIFEST_JSON)

    @property
    def state_files(self) -> tuple[Path, ...]:
        return (self.discovery_state, self.metadata_state, self.reviews_state, self.appdetails_cache,
                self.steamspy_cache)

    @property
    def output_dirs(self) -> tuple[Path, ...]:
        return (self.base, self.work, self.discovery_out, self.metadata_out, self.reviews_out)


class WorkspaceBusy(Exception):
    pass


class WorkspaceCorrupt(Exception):
    pass


@contextmanager
def workspace_lock(ws: Workspace):
    """Exclusive, non-blocking lock so two crawls never write the same state."""
    ws.work.mkdir(parents=True, exist_ok=True)
    fd = os.open(ws.lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkspaceBusy(f"another crawler process holds {ws.lock_file}") from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _atomic_write_bytes(path: Path, data: bytes) -> None:
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


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class CrawlStatus:
    """``work/crawl_status.json``: the last outcome of every stage, written atomically."""

    def __init__(self, ws: Workspace, now: Callable[[], dt.datetime] = _now):
        self.ws = ws
        self._now = now

    def load(self) -> dict[str, Any]:
        try:
            return json.loads(self.ws.status_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"stages": {}}

    def _save(self, status: dict[str, Any]) -> None:
        status["updated_at"] = iso(self._now())
        _atomic_write_bytes(self.ws.status_json, (json.dumps(status, indent=2, sort_keys=True) + "\n").encode())

    @contextmanager
    def stage(self, name: str):
        """Record running -> finished/failed for a stage; the body fills ``summary``."""
        status = self.load()
        entry = {"state": "running", "started_at": iso(self._now()), "summary": {}}
        status.setdefault("stages", {})[name] = entry
        self._save(status)
        summary: dict[str, Any] = {}
        try:
            yield summary
        except BaseException as exc:
            entry.update(state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                         finished_at=iso(self._now()), summary=summary, error=f"{type(exc).__name__}: {exc}")
            self._save(status)
            raise
        entry.update(state="finished" if summary.get("exit_code", 0) in (EXIT_OK,) else "incomplete",
                     finished_at=iso(self._now()), summary=summary)
        self._save(status)


def recover(ws: Workspace) -> list[str]:
    """Safe checkpoint recovery: drop leftover temp files, verify every SQLite state file.

    Temp files (``.<name>.tmp``) only ever exist between a write and its atomic
    rename, so a leftover one is an interrupted write and is safe to delete.
    A state file that fails ``PRAGMA quick_check`` is never repaired or
    deleted automatically.
    """
    actions = []
    for d in ws.output_dirs:
        if d.is_dir():
            for tmp in d.glob(".*.tmp"):
                tmp.unlink(missing_ok=True)
                actions.append(f"removed interrupted temp file {tmp}")
    for db in ws.state_files:
        if db.exists():
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    result = con.execute("PRAGMA quick_check").fetchone()[0]
                finally:
                    con.close()
            except sqlite3.DatabaseError as exc:
                result = str(exc)
            if result != "ok":
                raise WorkspaceCorrupt(f"{db} failed integrity check ({result}); move it aside to start over")
    return actions


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Shared construction
# --------------------------------------------------------------------------


@dataclass
class Context:
    ws: Workspace
    environ: Mapping[str, str]
    transport: SteamTransport | None = None
    log: Callable[[str], None] = print
    now: Callable[[], dt.datetime] = _now
    today: Callable[[], dt.date] = dt.date.today
    cache_ttl: dt.timedelta = DEFAULT_APPDETAILS_TTL
    last_summary: dict[str, Any] = field(default_factory=dict)

    def get_transport(self) -> SteamTransport:
        if self.transport is None:
            self.transport = SteamTransport()
        return self.transport

    def legacy_ids(self) -> frozenset[int]:
        return discovery.load_legacy_appids(self.ws.legacy_csv)

    def key(self):
        return load_api_key(self.environ, required=False)


def _store(ctx: Context, cache: ResponseCache) -> SteamStoreAPI:
    return SteamStoreAPI(ctx.get_transport(), cache=cache, ttl=ctx.cache_ttl, now=ctx.now)


def eligible_appids(ws: Workspace, legacy_ids: frozenset[int]) -> tuple[list[int], Any]:
    """Eligible AppIDs under the latest discovery run's window (empty without discovery state)."""
    if not ws.discovery_state.exists():
        return [], None
    with DiscoveryState(ws.discovery_state) as state:
        run = state.latest_run()
        if run is None:
            return [], None
        ids = [app.appid for app, d, _, _ in discovery.decide_all(state, run.window, legacy_ids)
               if d is not None and d.status == ELIGIBLE]
        return ids, run


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------


def audit(ctx: Context) -> int:
    ws, log = ctx.ws, ctx.log
    problems = 0

    def line(status: str, name: str, detail: str) -> None:
        nonlocal problems
        problems += status == "FAIL"
        log(f"[{status:^4}] {name}: {detail}")

    log("Steam4CGS audit (read-only, no network)")
    if not ws.legacy_csv.exists():
        line("FAIL", "legacy CSV", f"{ws.legacy_csv} not found")
    else:
        digest = sha256_file(ws.legacy_csv)
        line("OK" if digest == LEGACY_SHA256 else "FAIL", "legacy CSV sha256",
             digest if digest == LEGACY_SHA256 else f"{digest} != expected {LEGACY_SHA256}")
        violations = schema.validate_file(ws.legacy_csv)
        rows = sum(1 for _ in schema.read_rows(ws.legacy_csv)) if not violations else "?"
        line("OK" if not violations and rows == LEGACY_ROWS else "FAIL", "legacy CSV contract",
             f"{rows} rows, {len(violations)} violation(s)")
    for r in preflight.offline_checks(ctx.environ)[0]:
        # A missing key limits what can be crawled but is not an audit failure.
        status = "WARN" if r.status == preflight.FAIL and r.name == "STEAM_API_KEY" else r.status
        line(status, r.name, r.detail)
    try:
        recover_needed = [p for d in ws.output_dirs if d.is_dir() for p in d.glob(".*.tmp")]
        for db in ws.state_files:
            if db.exists():
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                result = con.execute("PRAGMA quick_check").fetchone()[0]
                con.close()
                line("OK" if result == "ok" else "FAIL", f"state {db.name}", result)
        if recover_needed:
            line("WARN", "interrupted writes", f"{len(recover_needed)} temp file(s); removed by the next run")
    except sqlite3.DatabaseError as exc:
        line("FAIL", "state", str(exc))
    stages = CrawlStatus(ws).load().get("stages", {})
    for name, entry in sorted(stages.items()):
        state = entry.get("state")
        line("WARN" if state in ("running", "failed", "interrupted") else "OK", f"stage {name}",
             f"{state} at {entry.get('finished_at') or entry.get('started_at')}")
    if ws.discovery_state.exists():
        with DiscoveryState(ws.discovery_state) as state:
            unfinished = state.unfinished_run()
        if unfinished:
            line("WARN", "discovery", f"run #{unfinished.run_id} unfinished; continue with --resume")
    if ws.new_csv.exists():
        line("OK", "outputs", "present; run 'validate' to check them")
    return EXIT_ERROR if problems else EXIT_OK


# --------------------------------------------------------------------------
# discover
# --------------------------------------------------------------------------


def discover(ctx: Context, opts: discovery.DiscoveryOptions, *, dry_run: bool = False) -> int:
    ws, log = ctx.ws, ctx.log
    legacy = ctx.legacy_ids()
    if dry_run:
        return _discover_plan(ctx, opts, legacy)
    with ResponseCache(ws.appdetails_cache) as cache, DiscoveryState(ws.discovery_state) as state:
        transport = ctx.get_transport()
        runner = discovery.Discovery(
            state, web_factory=lambda: SteamWebAPI(transport, load_api_key(ctx.environ)),
            store=_store(ctx, cache), legacy_ids=legacy, today=ctx.today, now=ctx.now, log=log)
        report, code = None, EXIT_OK
        try:
            report = runner.run(opts)
            if report.stop_reason in (discovery.RATE_LIMITED, discovery.TOO_MANY_FAILURES):
                code = EXIT_TEMPFAIL
        except discovery.DiscoveryConfigError as exc:
            log(f"error: {exc}")
            return EXIT_USAGE
        except (MissingCredentialError, InvalidCredentialError, SteamAuthError) as exc:
            log(f"error: {exc}")
            code = EXIT_ERROR
        except SteamError as exc:
            log(f"error: {type(exc).__name__}: {exc}")
            code = EXIT_TEMPFAIL
        run = state.get_run(report.run_id) if report else state.unfinished_run()
        if run is not None:
            counts = discovery.export(state, ws.discovery_out, run, legacy, report)
            log("discovery: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        if report is not None:
            log(f"discovery run #{report.run_id} ({report.mode}) {report.stop_reason}: {report.lookups} lookup(s), "
                f"{report.lookup_failures} failure(s), {report.pending} pending")
        if state.unfinished_run() is not None:
            log("discovery is unfinished; continue with --resume")
        ctx.last_summary = {"stop_reason": report.stop_reason if report else None,
                            "lookups": report.lookups if report else 0,
                            "requests_sent": transport.requests_sent, "exit_code": code}
        return code


def _discover_plan(ctx: Context, opts: discovery.DiscoveryOptions, legacy: frozenset[int]) -> int:
    ws, log = ctx.ws, ctx.log
    log("dry run: no requests, no writes")
    if not ws.discovery_state.exists():
        log("discover: fresh workspace; would start a "
            + ("seed run of %d AppID(s)" % len(set(opts.seed_appids)) if opts.seed_appids
               else "full GetAppList crawl (needs STEAM_API_KEY)"))
        return EXIT_OK
    with DiscoveryState(ws.discovery_state) as state:
        run = state.unfinished_run()
        if run is not None and not (opts.resume or opts.restart):
            log(f"discover: run #{run.run_id} is unfinished; this would fail without --resume/--restart")
            return EXIT_USAGE
        target = run if (run is not None and opts.resume) else None
        if target is not None:
            pending = state.pending(target, legacy)
            log(f"discover: would resume run #{target.run_id} ({target.mode}, window {target.window}), "
                f"catalog {'complete' if target.catalog_complete else f'at cursor {target.catalog_cursor}'}, "
                f"{len(pending)} appdetails lookup(s) pending"
                + (f", at most {opts.limit} this time" if opts.limit is not None else ""))
        else:
            log("discover: would start a new " + ("seed" if opts.seed_appids else
                                                ("incremental" if opts.incremental else "full")) + " run")
    return EXIT_OK


# --------------------------------------------------------------------------
# crawl
# --------------------------------------------------------------------------


def crawl(ctx: Context, *, limit: int | None = None, refresh: bool = False, enable_steamspy: bool = False,
          dry_run: bool = False, review_settings: rv.ReviewSettings = rv.ReviewSettings()) -> int:
    ws, log = ctx.ws, ctx.log
    legacy = ctx.legacy_ids()
    eligible, run = eligible_appids(ws, legacy)
    if run is None:
        log("error: no discovery state; run 'discover' first")
        return EXIT_USAGE
    tags_source = TAGS_STEAMSPY if enable_steamspy else TAGS_STEAM
    if dry_run:
        done = set()
        if ws.metadata_state.exists():
            with ExtractionState(ws.metadata_state) as st:
                complete, partial, failed = st.status()
                done = complete
        reviewed = set(rv.load_records(ws.reviews_state)) if ws.reviews_state.exists() else set()
        todo = [a for a in eligible if refresh or a not in done]
        rtodo = [a for a in eligible if refresh or a not in reviewed]
        cap = (lambda n: min(n, limit)) if limit is not None else (lambda n: n)
        log("dry run: no requests, no writes")
        log(f"crawl: {len(eligible)} eligible (window {run.window}); metadata would fetch {cap(len(todo))} of "
            f"{len(todo)} pending, reviews {cap(len(rtodo))} of {len(rtodo)}; tags source: {tags_source}")
        return EXIT_OK

    try:
        key = ctx.key()
    except InvalidCredentialError as exc:
        log(f"error: {exc}")
        return EXIT_ERROR
    transport = ctx.get_transport()
    code = EXIT_OK
    summary: dict[str, Any] = {"eligible": len(eligible), "tags_source": tags_source}
    spy_cache = spy.SteamSpyCache(ws.steamspy_cache) if enable_steamspy else None
    try:
        with ResponseCache(ws.appdetails_cache) as cache, ExtractionState(ws.metadata_state) as xstate:
            steamspy = spy.SteamSpyClient(transport, spy_cache, now=ctx.now) if spy_cache else None
            store = _store(ctx, cache)
            extractor = extract.Extractor(xstate, web=SteamWebAPI(transport, key), store=store, legacy_ids=legacy,
                                          tags_source=tags_source, steamspy=steamspy, now=ctx.now, log=log)
            xr = extractor.run(eligible, limit=limit, refresh=refresh)
            summary["metadata"] = dataclasses.asdict(xr) | {"appdetails_network_requests": store.network_requests}
            log(f"metadata {xr.stop_reason}: {xr.attempted} fetched ({store.network_requests} appdetails over the "
                f"network, the rest from cache), {xr.extracted} complete, {xr.partial} partial, "
                f"{xr.failed} failed, {xr.pending} pending")
            if xr.stop_reason == "rate_limited":
                code = EXIT_TEMPFAIL
    finally:
        if spy_cache is not None:
            spy_cache.close()

    with rv.ReviewState(ws.reviews_state) as rstate:
        collector = rv.ReviewCollector(rstate, SteamWebAPI(transport, key), settings=review_settings,
                                       legacy_ids=legacy, now=ctx.now, log=log)
        try:
            rr = collector.run(eligible, limit=limit, refresh=refresh)
            summary["reviews"] = {"available": True, **dataclasses.asdict(rr)}
            log(f"reviews {rr.stop_reason}: {rr.attempted} requested, "
                + ", ".join(f"{k}={v}" for k, v in rr.statuses.items() if v))
            if rr.stop_reason == "rate_limited":
                code = EXIT_TEMPFAIL
        except rv.ReviewEndpointUnavailable as exc:
            summary["reviews"] = {"available": False, "reason": str(exc)}
            log(f"warning: review summaries skipped: {exc}; sentiment stays blank")
        except SteamAuthError as exc:
            summary["reviews"] = {"available": False, "reason": str(exc)}
            log(f"error: {exc}")
            code = EXIT_ERROR
        rstate.set_meta("last_crawl_available", summary["reviews"]["available"])
    summary.update(requests_sent=transport.requests_sent, exit_code=code)
    ctx.last_summary = summary
    return code


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


def _csv_bytes(rows: Iterable[Sequence[str]]) -> bytes:
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator=schema.LINE_TERMINATOR, quoting=csv.QUOTE_MINIMAL).writerows(rows)
    return buf.getvalue().encode(schema.ENCODING)


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def build_outputs(ws: Workspace, legacy: frozenset[int]) -> dict[str, Any]:
    """Compute every final output in memory (no writes)."""
    eligible, run = eligible_appids(ws, legacy)
    eligible_set = set(eligible)
    reviews = rv.load_records(ws.reviews_state)
    stored: list[tuple[dict, list]] = []
    xfailures: list[dict] = []
    if ws.metadata_state.exists():
        with ExtractionState(ws.metadata_state) as xstate:
            stored = [with_review(r, p, reviews.get(int(r["id"]))) for r, p in xstate.rows()]
            xfailures = xstate.failures()
    new = sorted((rp for rp in stored if int(rp[0]["id"]) in eligible_set and int(rp[0]["id"]) not in legacy),
                 key=lambda rp: int(rp[0]["id"]))
    new_rows = [row for row, _ in new]
    for row in new_rows:
        violations = schema.validate_row(row)
        if violations:
            raise schema.SchemaError(violations)

    failed: list[dict[str, str]] = []
    if ws.discovery_state.exists():
        with DiscoveryState(ws.discovery_state) as dstate:
            for f in dstate.all_failures().values():
                failed.append({"appid": str(f.appid), "stage": "discover", "error": f.error, "detail": f.detail,
                               "attempts": str(f.attempts), "last_attempt_at": f.last_attempt_at})
            disc_counts = {}
            if run is not None:
                for app, d, _, _ in discovery.decide_all(dstate, run.window, legacy):
                    status = d.status if d else discovery.PENDING
                    disc_counts[status] = disc_counts.get(status, 0) + 1
    else:
        disc_counts = {}
    for f in xfailures:
        failed.append({"appid": f["appid"], "stage": "crawl:metadata", "error": f["error"], "detail": f["detail"],
                       "attempts": f["attempts"], "last_attempt_at": f["last_attempt_at"]})
    for r in sorted(reviews.values(), key=lambda r: r.appid):
        if r.status == rv.API_ERROR:
            failed.append({"appid": str(r.appid), "stage": "crawl:reviews", "error": r.status, "detail": r.detail,
                           "attempts": "", "last_attempt_at": r.retrieved_at})
    failed.sort(key=lambda f: (int(f["appid"]), f["stage"]))
    missing = sorted(eligible_set - {int(r["id"]) for r in new_rows} - legacy)
    return {
        "run": run, "eligible": eligible, "new_rows": new_rows,
        "provenance": [p for _, prov in new for p in prov], "failed": failed,
        "discovery_counts": disc_counts, "reviews": reviews, "not_yet_crawled": missing,
    }


def export(ctx: Context, *, dry_run: bool = False) -> int:
    ws, log = ctx.ws, ctx.log
    legacy = ctx.legacy_ids()
    legacy_bytes = ws.legacy_csv.read_bytes()
    if hashlib.sha256(legacy_bytes).hexdigest() != LEGACY_SHA256:
        log(f"error: {ws.legacy_csv} does not match the expected sha256; refusing to build the merged CSV")
        return EXIT_ERROR
    out = build_outputs(ws, legacy)
    new_rows = out["new_rows"]
    if dry_run:
        log("dry run: no writes")
        log(f"export: would write {len(new_rows)} new game(s), merged {LEGACY_ROWS + len(new_rows)} rows, "
            f"{len(out['provenance'])} provenance rows, {len(out['failed'])} failed AppID row(s); "
            f"{len(out['not_yet_crawled'])} eligible game(s) not crawled yet")
        return EXIT_OK

    # Intermediate per-module exports (review statistics, prices, tag votes, Early Access evidence).
    if ws.metadata_state.exists():
        with ExtractionState(ws.metadata_state) as xstate:
            extract.export(xstate, ws.metadata_out, out["reviews"])
    if ws.reviews_state.exists():
        with rv.ReviewState(ws.reviews_state) as rstate:
            any_record = next(iter(out["reviews"].values()), None)
            settings = (rv.ReviewSettings(any_record.review_language, any_record.purchase_type)
                        if any_record else rv.ReviewSettings())
            rv.export(rstate, ws.reviews_out, settings)

    rows = [[r[c] for c in schema.COLUMNS] for r in new_rows]
    header = _csv_bytes([schema.COLUMNS])
    new_bytes = header + _csv_bytes(rows)
    # Merged: the legacy file byte-for-byte, then CRLF and the new rows (the legacy file has no final newline).
    merged_bytes = legacy_bytes + (schema.LINE_TERMINATOR.encode() + _csv_bytes(rows) if rows else b"")
    prov_buf = io.StringIO(newline="")
    w = csv.DictWriter(prov_buf, fieldnames=PROVENANCE_FIELDS, lineterminator=schema.LINE_TERMINATOR)
    w.writeheader()
    w.writerows(out["provenance"])
    fail_buf = io.StringIO(newline="")
    w = csv.DictWriter(fail_buf, fieldnames=FAILED_FIELDS, lineterminator=schema.LINE_TERMINATOR)
    w.writeheader()
    w.writerows(out["failed"])

    files = {ws.new_csv: new_bytes, ws.merged_csv: merged_bytes,
             ws.provenance_csv: prov_buf.getvalue().encode(), ws.failed_csv: fail_buf.getvalue().encode()}
    for path, data in files.items():
        _atomic_write_bytes(path, data)

    manifest = build_manifest(ws, out, {p: hashlib.sha256(d).hexdigest() for p, d in files.items()},
                              legacy_bytes, ctx.now)
    _atomic_write_bytes(ws.manifest, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    log(f"export: {len(new_rows)} new game(s) -> {ws.new_csv.name}; merged {LEGACY_ROWS + len(new_rows)} rows; "
        f"{len(out['provenance'])} provenance rows; {len(out['failed'])} failed AppID row(s)")
    ctx.last_summary = {"new_games": len(new_rows), "failed_rows": len(out["failed"]), "exit_code": EXIT_OK}
    return EXIT_OK


def build_manifest(ws: Workspace, out: Mapping[str, Any], hashes: Mapping[Path, str], legacy_bytes: bytes,
                   now: Callable[[], dt.datetime]) -> dict[str, Any]:
    run = out["run"]
    reviews = out["reviews"]
    review_statuses: dict[str, int] = {}
    for r in reviews.values():
        review_statuses[r.status] = review_statuses.get(r.status, 0) + 1
    settings = sorted({(r.review_language, r.purchase_type) for r in reviews.values()})
    tags_sources: dict[str, int] = {}
    early: dict[str, int] = {}
    if ws.metadata_state.exists():
        with ExtractionState(ws.metadata_state) as xstate:
            new_ids = {int(r["id"]) for r in out["new_rows"]}
            for appid, e in xstate.extras().items():
                if appid in new_ids:
                    tags_sources[e.get("tags_source", TAGS_STEAM)] = tags_sources.get(e.get("tags_source", TAGS_STEAM), 0) + 1
                    s = e.get("early_access", {}).get("status", "unknown")
                    early[s] = early.get(s, 0) + 1
    rel = lambda p: os.path.relpath(p, ws.base)  # noqa: E731
    return {
        "tool": {"name": "steam4cgs-crawler", "version": "0.1.0", "git_commit": _git_commit(),
                 "python": platform.python_version(), "requests": requests.__version__},
        "generated_at": iso(now()),
        "window": None if run is None else {"start": run.window.start.isoformat(), "end": run.window.end.isoformat()},
        "discovery": None if run is None else {"run_id": run.run_id, "mode": run.mode, "status": run.status,
                                               "started_at": run.started_at, "finished_at": run.finished_at,
                                               "counts": out["discovery_counts"]},
        "legacy": {"path": rel(ws.legacy_csv), "sha256": hashlib.sha256(legacy_bytes).hexdigest(), "rows": LEGACY_ROWS,
                   "preserved_in_merged": True},
        "crawl": {"eligible": len(out["eligible"]), "new_games": len(out["new_rows"]),
                  "eligible_not_yet_crawled": len(out["not_yet_crawled"]), "tags_source": tags_sources,
                  "early_access_status": early},
        "reviews": {"endpoint": f"{rv.INTERFACE}/{rv.METHOD}/v{rv.VERSION}", "statuses": review_statuses,
                    "settings": [{"review_language": l, "purchase_type": p} for l, p in settings],
                    "sentiment_filled": sum(1 for r in out["new_rows"] if r["sentiment"])},
        "outputs": {rel(p): {"sha256": h, "rows": _row_count(p, out)} for p, h in sorted(hashes.items())},
        "sources": {
            "catalog": "https://api.steampowered.com/IStoreService/GetAppList/v1/ (key required)",
            "metadata": "https://store.steampowered.com/api/appdetails (cc=us, l=english; undocumented)",
            "store_item": "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/ (tags, is_early_access)",
            "tag_names": "https://api.steampowered.com/IStoreService/GetTagList/v1/",
            "reviews": "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/",
            "steamspy": "https://steamspy.com/api.php?request=appdetails (optional, third party)",
        },
        "intermediate": {"review_statistics": rel(ws.reviews_out / "review_summaries.csv"),
                         "prices": rel(ws.metadata_out / "prices.csv"),
                         "tag_votes": rel(ws.metadata_out / "tag_votes.csv"),
                         "early_access": rel(ws.metadata_out / "early_access.csv"),
                         "discovery": rel(ws.discovery_out)},
        "stages": CrawlStatus(ws).load().get("stages", {}),
        "limitations": list(LIMITATIONS),
    }


def _row_count(path: Path, out: Mapping[str, Any]) -> int:
    name = path.name
    if name == NEW_CSV:
        return len(out["new_rows"])
    if name == MERGED_CSV:
        return LEGACY_ROWS + len(out["new_rows"])
    if name == PROVENANCE_CSV:
        return len(out["provenance"])
    return len(out["failed"])


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


def validate(ctx: Context) -> int:
    ws, log = ctx.ws, ctx.log
    problems: list[str] = []
    check = lambda ok, msg: ok or problems.append(msg)  # noqa: E731

    for p in (ws.new_csv, ws.merged_csv, ws.provenance_csv, ws.manifest, ws.failed_csv):
        check(p.exists(), f"missing output {p}")
    if problems:
        for p in problems:
            log(f"[FAIL] {p}")
        return EXIT_ERROR

    legacy_bytes = ws.legacy_csv.read_bytes()
    check(hashlib.sha256(legacy_bytes).hexdigest() == LEGACY_SHA256, "legacy CSV checksum changed")
    legacy_rows = list(csv.reader(io.StringIO(legacy_bytes.decode("utf-8"), newline="")))
    legacy_ids = {int(r[0]) for r in legacy_rows[1:]}

    # 1. new CSV: exact 13 columns, contract-valid, new AppIDs only, no duplicates
    new_bytes = ws.new_csv.read_bytes()
    check(new_bytes.split(b"\r\n", 1)[0] == legacy_bytes.split(b"\r\n", 1)[0], "new CSV header differs from legacy")
    for v in schema.validate_file(ws.new_csv)[:10]:
        problems.append(f"new CSV: {v}")
    new_rows = list(csv.reader(io.StringIO(new_bytes.decode("utf-8"), newline="")))[1:]
    new_ids = [int(r[0]) for r in new_rows if r and r[0].isdigit()]
    check(all(len(r) == len(schema.COLUMNS) for r in new_rows), "new CSV rows must have exactly 13 fields")
    check(len(new_ids) == len(set(new_ids)), "new CSV has duplicate AppIDs")
    check(not set(new_ids) & legacy_ids, f"new CSV contains legacy AppIDs: {sorted(set(new_ids) & legacy_ids)[:5]}")
    eligible, _ = eligible_appids(ws, frozenset(legacy_ids))
    if eligible:
        check(set(new_ids) <= set(eligible), "new CSV contains AppIDs that discovery did not mark eligible")

    # 2. merged: legacy bytes unchanged as a prefix, then exactly the new rows
    merged_bytes = ws.merged_csv.read_bytes()
    check(merged_bytes[:len(legacy_bytes)] == legacy_bytes, "merged CSV does not start with the legacy file byte-for-byte")
    tail = merged_bytes[len(legacy_bytes):]
    check(tail == b"" if not new_rows else tail.startswith(b"\r\n"), "merged CSV: unexpected bytes after legacy rows")
    merged_rows = list(csv.reader(io.StringIO(merged_bytes.decode("utf-8"), newline="")))
    check(merged_rows[:len(legacy_rows)] == legacy_rows, "merged CSV legacy rows differ")
    check(merged_rows[len(legacy_rows):] == new_rows, "merged CSV appended rows differ from the new CSV")
    merged_ids = [int(r[0]) for r in merged_rows[1:]]
    check(len(merged_ids) == len(set(merged_ids)), "merged CSV has duplicate AppIDs")
    for v in schema.validate_file(ws.merged_csv)[:10]:
        problems.append(f"merged CSV: {v}")

    # 3. provenance: one row per (new AppID, column) agreeing with the cells
    with open(ws.provenance_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        check(tuple(reader.fieldnames or ()) == PROVENANCE_FIELDS, "provenance header differs")
        prov = {(int(r["appid"]), r["field"]): r for r in reader}
    expected = {(int(r[0]), c): r[i] for r in new_rows for i, c in enumerate(schema.COLUMNS)}
    check(set(prov) == set(expected), f"provenance covers {len(prov)} cells, expected {len(expected)}")
    bad = [k for k, v in expected.items() if k in prov and prov[k]["value"] != v]
    check(not bad, f"provenance values disagree with the new CSV: {bad[:5]}")

    # 4. manifest hashes and counts
    try:
        manifest = json.loads(ws.manifest.read_text(encoding="utf-8"))
        for rel_path, info in manifest["outputs"].items():
            path = ws.base / rel_path
            check(path.exists() and sha256_file(path) == info["sha256"], f"manifest hash mismatch for {rel_path}")
        check(manifest["legacy"]["sha256"] == LEGACY_SHA256, "manifest legacy checksum differs")
        check(manifest["crawl"]["new_games"] == len(new_rows), "manifest new_games count differs")
    except (ValueError, KeyError, TypeError) as exc:
        problems.append(f"manifest unreadable: {exc}")

    # 5. failed AppIDs file
    with open(ws.failed_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        check(tuple(reader.fieldnames or ()) == FAILED_FIELDS, "failed_appids.csv header differs")
        metadata_failed = {int(r["appid"]) for r in reader if r["stage"] == "crawl:metadata"}
    check(not metadata_failed & set(new_ids), "an AppID is both in the new CSV and failed at the metadata stage")

    log(f"validate: new={len(new_rows)} merged={len(merged_rows) - 1} provenance={len(prov)}")
    for p in problems:
        log(f"[FAIL] {p}")
    if not problems:
        log("[ OK ] all output checks passed")
    ctx.last_summary = {"problems": problems, "exit_code": EXIT_ERROR if problems else EXIT_OK}
    return EXIT_ERROR if problems else EXIT_OK


# --------------------------------------------------------------------------
# stage runner and run
# --------------------------------------------------------------------------


def run_stage(ctx: Context, name: str, fn: Callable[[], int], *, mutating: bool = True) -> int:
    """Run one stage under the workspace lock with recovery and persistent status."""
    if not mutating:
        return fn()
    with workspace_lock(ctx.ws):
        for action in recover(ctx.ws):
            ctx.log(f"recovery: {action}")
        with CrawlStatus(ctx.ws, ctx.now).stage(name) as summary:
            ctx.last_summary = {}
            code = fn()
            summary.update(ctx.last_summary, exit_code=code)
        return code


def _discovery_finished(ws: Workspace) -> bool:
    """True when discovery state exists, has a run, and none is unfinished."""
    if not ws.discovery_state.exists():
        return False
    with DiscoveryState(ws.discovery_state) as state:
        return state.unfinished_run() is None and state.latest_run() is not None


_SEVERITY = {EXIT_OK: 0, EXIT_TEMPFAIL: 1, EXIT_INTERRUPTED: 2, EXIT_USAGE: 3, EXIT_ERROR: 4}


def worst(*codes: int) -> int:
    return max(codes, key=lambda c: _SEVERITY.get(c, 5))


def run_all(ctx: Context, opts: discovery.DiscoveryOptions, *, limit: int | None = None, refresh: bool = False,
            enable_steamspy: bool = False, dry_run: bool = False) -> int:
    """discover -> crawl -> export -> validate. A discovery that stops early (limit, rate
    limit) still lets the eligible games found so far be crawled and exported."""
    mutating = not dry_run
    if opts.resume and _discovery_finished(ctx.ws):
        ctx.log("discover: no unfinished discovery run; resuming from the crawl stage")
        d = EXIT_OK
    else:
        d = run_stage(ctx, "discover", lambda: discover(ctx, opts, dry_run=dry_run), mutating=mutating)
    if d in (EXIT_ERROR, EXIT_USAGE):
        return d
    if dry_run and not ctx.ws.discovery_state.exists():
        ctx.log("crawl/export: nothing to plan until discovery has run")
        return d
    c = run_stage(ctx, "crawl", lambda: crawl(ctx, limit=limit, refresh=refresh, enable_steamspy=enable_steamspy,
                                             dry_run=dry_run), mutating=mutating)
    if c in (EXIT_ERROR, EXIT_USAGE):
        return c
    e = run_stage(ctx, "export", lambda: export(ctx, dry_run=dry_run), mutating=mutating)
    if dry_run or e != EXIT_OK:
        return worst(d, c, e)
    v = run_stage(ctx, "validate", lambda: validate(ctx))
    return worst(d, c, e, v)

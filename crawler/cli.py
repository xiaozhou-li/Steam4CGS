"""Command-line entry point for the resumable Steam game crawler.

Pipeline commands (shared workspace: --base-dir, --work-dir, --legacy-csv):

    python -m crawler audit
    python -m crawler preflight [--live [--steamspy]] [--appid N]
    python -m crawler discover  [--start-date D] [--end-date D] [--limit N] [--resume | --restart]
                                [--incremental] [--appid N ... | --appids-file F] [--dry-run]
    python -m crawler crawl     [--limit N] [--refresh] [--enable-steamspy] [--resume] [--dry-run]
    python -m crawler export    [--dry-run]
    python -m crawler validate
    python -m crawler run       (discover + crawl + export + validate; all of the options above)

Lower-level tools: ``extract`` and ``reviews`` (see docs/metadata.md, docs/reviews.md).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from crawler import discovery, extract, pipeline, preflight
from crawler import reviews as rv
from crawler import steamspy as spy
from crawler.credentials import InvalidCredentialError, load_api_key
from crawler.eligibility import DateWindow
from crawler.metadata import TAG_SOURCES, TAGS_STEAM, TAGS_STEAMSPY
from crawler.steam_api import SteamStoreAPI, SteamWebAPI
from crawler.transport import SteamAuthError, SteamError, SteamTransport

ROOT = pipeline.ROOT
WORK = ROOT / "work"
DEFAULT_LEGACY_CSV = ROOT / "steamproducts2020.csv"
# Defaults of the lower-level extract/reviews tools: the pipeline's workspace files.
DEFAULT_OUT = WORK / "discovery"
DEFAULT_METADATA_STATE = WORK / "metadata.sqlite3"
DEFAULT_METADATA_OUT = WORK / "metadata"
DEFAULT_REVIEWS_STATE = WORK / "reviews.sqlite3"
DEFAULT_REVIEWS_OUT = WORK / "reviews"
DEFAULT_STEAMSPY_CACHE = WORK / "cache" / "steamspy.sqlite3"

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_TEMPFAIL, EXIT_INTERRUPTED = (
    pipeline.EXIT_OK, pipeline.EXIT_ERROR, pipeline.EXIT_USAGE, pipeline.EXIT_TEMPFAIL, pipeline.EXIT_INTERRUPTED)


def _date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {text!r}") from None


def _non_negative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def _workspace_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--base-dir", type=Path, default=ROOT, help="where the final outputs go (default: project root)")
    p.add_argument("--work-dir", type=Path, help="checkpoints, caches and intermediate files (default: BASE/work)")
    p.add_argument("--legacy-csv", type=Path, default=DEFAULT_LEGACY_CSV, help="original dataset (read-only)")


def _discover_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--start-date", "--start", dest="start", type=_date,
                   help=f"first release date to include (default: {discovery.DEFAULT_START})")
    p.add_argument("--end-date", "--end", dest="end", type=_date, help="last release date to include (default: today)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true", help="continue the unfinished discovery run from its checkpoint")
    mode.add_argument("--restart", action="store_true", help="abandon an unfinished discovery run and start anew")
    p.add_argument("--incremental", action="store_true",
                   help="only catalog entries modified since the last finished catalog run")
    p.add_argument("--recheck-unreleased", action="store_true",
                   help="re-fetch apps previously seen as unreleased or without an exact release date")
    p.add_argument("--page-size", type=int, default=discovery.DEFAULT_PAGE_SIZE, help="GetAppList max_results")
    seeds = p.add_mutually_exclusive_group()
    seeds.add_argument("--appid", type=int, action="append", dest="seed_appids",
                       help="seed mode: evaluate these AppIDs instead of the GetAppList catalog (no key needed)")
    seeds.add_argument("--appids-file", type=Path, help="seed mode: one AppID per line ('#' comments allowed)")


def _crawl_args(p: argparse.ArgumentParser, *, resume: bool) -> None:
    p.add_argument("--refresh", action="store_true",
                   help="re-fetch games already crawled and bypass the response caches")
    p.add_argument("--enable-steamspy", action="store_true", help="take tags from SteamSpy (third party, cached)")
    p.add_argument("--language", default="all", help="review-summary language setting (default: %(default)s)")
    p.add_argument("--purchase-type", choices=rv.PURCHASE_TYPES, default="all",
                   help="review-summary purchase-type setting (default: %(default)s)")
    if resume:
        p.add_argument("--resume", action="store_true",
                       help="continue from the checkpoints (crawl always does; accepted for symmetry)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crawler")
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser("audit", help="read-only checks: legacy CSV, credentials hygiene, workspace state")
    _workspace_args(a)

    p = sub.add_parser("preflight", help="check configuration; optionally make a few live API calls")
    p.add_argument("--live", action="store_true",
                   help=f"make at most {preflight.LIVE_MAX_REQUESTS} real requests to Steam (one app id)")
    p.add_argument("--steamspy", action="store_true", help="with --live, also make one SteamSpy request")
    p.add_argument("--appid", type=int, default=preflight.DEFAULT_LIVE_APPID,
                   help="app id for the live reviews/appdetails checks (default: %(default)s)")

    d = sub.add_parser("discover", help="find games released in the window that are not in the legacy CSV")
    _workspace_args(d)
    _discover_args(d)
    d.add_argument("--limit", type=_non_negative, help="maximum appdetails lookups in this invocation")
    d.add_argument("--dry-run", action="store_true", help="show what would happen; no requests, no writes")

    c = sub.add_parser("crawl", help="metadata and review summaries for eligible games")
    _workspace_args(c)
    _crawl_args(c, resume=True)
    c.add_argument("--limit", type=_non_negative, help="maximum games fetched per stage in this invocation")
    c.add_argument("--dry-run", action="store_true", help="show what would happen; no requests, no writes")

    e = sub.add_parser("export", help="write the final CSVs, provenance, manifest and failed AppIDs")
    _workspace_args(e)
    e.add_argument("--dry-run", action="store_true", help="compute the outputs without writing them")

    v = sub.add_parser("validate", help="check every output against the contract")
    _workspace_args(v)

    r = sub.add_parser("run", help="discover, crawl, export and validate")
    _workspace_args(r)
    _discover_args(r)
    _crawl_args(r, resume=False)
    r.add_argument("--limit", type=_non_negative,
                   help="maximum lookups per stage (discovery appdetails, metadata, reviews) in this invocation")
    r.add_argument("--dry-run", action="store_true", help="show what would happen; no requests, no writes")

    x = sub.add_parser("extract", help="(lower level) fetch and normalize Store metadata for AppIDs")
    src = x.add_mutually_exclusive_group()
    src.add_argument("--input", type=Path, default=DEFAULT_OUT / "eligible.csv",
                     help="eligible.csv from discovery (default: %(default)s)")
    src.add_argument("--appid", type=int, action="append", help="extract specific AppID(s) instead of --input")
    x.add_argument("--limit", type=_non_negative, help="maximum AppIDs to fetch in this invocation")
    x.add_argument("--refresh", action="store_true", help="re-fetch AppIDs that were already extracted")
    x.add_argument("--state", type=Path, default=DEFAULT_METADATA_STATE, help="SQLite checkpoint file")
    x.add_argument("--out", type=Path, default=DEFAULT_METADATA_OUT,
                   help="directory for games.csv, provenance.csv and failed.csv")
    x.add_argument("--legacy-csv", type=Path, default=DEFAULT_LEGACY_CSV, help="original dataset (read-only)")
    x.add_argument("--reviews-state", type=Path, default=DEFAULT_REVIEWS_STATE,
                   help="review-summary state used for the sentiment column (default: %(default)s)")
    x.add_argument("--tags-source", choices=TAG_SOURCES, default=TAGS_STEAM,
                   help="steam: Store user tags (default); steamspy: SteamSpy tag votes (third party, optional)")
    x.add_argument("--steamspy-cache", type=Path, default=DEFAULT_STEAMSPY_CACHE,
                   help="SteamSpy response cache (default: %(default)s)")
    x.add_argument("--steamspy-ttl-hours", type=float, default=spy.DEFAULT_TTL.total_seconds() / 3600,
                   help="reuse cached SteamSpy responses younger than this (default: %(default)s)")

    rv_ = sub.add_parser("reviews", help="(lower level) review summaries via IUserReviewsService/GetAppReviews")
    rsrc = rv_.add_mutually_exclusive_group()
    rsrc.add_argument("--input", type=Path, default=DEFAULT_OUT / "eligible.csv",
                      help="eligible.csv from discovery (default: %(default)s)")
    rsrc.add_argument("--appid", type=int, action="append", help="collect specific AppID(s) instead of --input")
    rv_.add_argument("--limit", type=_non_negative, help="maximum summary requests in this invocation")
    rv_.add_argument("--refresh", action="store_true", help="re-collect summaries that already exist")
    rv_.add_argument("--language", default="all", help="review language setting (default: %(default)s)")
    rv_.add_argument("--purchase-type", choices=rv.PURCHASE_TYPES, default="all",
                     help="purchase-type setting (default: %(default)s)")
    rv_.add_argument("--state", type=Path, default=DEFAULT_REVIEWS_STATE, help="SQLite checkpoint file")
    rv_.add_argument("--out", type=Path, default=DEFAULT_REVIEWS_OUT, help="directory for review_summaries.csv")
    rv_.add_argument("--legacy-csv", type=Path, default=DEFAULT_LEGACY_CSV, help="original dataset (read-only)")
    return parser


def read_appids_file(path: Path) -> list[int]:
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        if not text.isdigit() or int(text) <= 0:
            raise ValueError(f"{path}:{n}: not a positive AppID: {text!r}")
        out.append(int(text))
    return out


def _context(args: argparse.Namespace, environ: Mapping[str, str], transport: SteamTransport | None) -> pipeline.Context:
    ws = pipeline.Workspace.at(args.base_dir, args.work_dir, args.legacy_csv)
    return pipeline.Context(ws, environ, transport=transport)


def _discovery_options(args: argparse.Namespace) -> discovery.DiscoveryOptions:
    window = None
    if args.start or args.end:
        window = DateWindow(args.start or discovery.DEFAULT_START, args.end or dt.date.today())
    seeds = args.seed_appids or (read_appids_file(args.appids_file) if args.appids_file else None)
    return discovery.DiscoveryOptions(
        window=window, limit=args.limit, incremental=args.incremental, resume=args.resume, restart=args.restart,
        recheck_unreleased=args.recheck_unreleased, page_size=args.page_size,
        seed_appids=tuple(seeds) if seeds else None)


def run_pipeline_command(args: argparse.Namespace, environ: Mapping[str, str],
                         transport: SteamTransport | None = None) -> int:
    ctx = _context(args, environ, transport)
    cmd = args.command
    try:
        if cmd == "audit":
            return pipeline.audit(ctx)
        if cmd == "validate":
            return pipeline.run_stage(ctx, "validate", lambda: pipeline.validate(ctx))
        if cmd == "export":
            return pipeline.run_stage(ctx, "export", lambda: pipeline.export(ctx, dry_run=args.dry_run),
                                      mutating=not args.dry_run)
        settings = None
        if cmd in ("crawl", "run"):
            settings = rv.ReviewSettings(language=args.language, purchase_type=args.purchase_type)
        if cmd == "crawl":
            return pipeline.run_stage(ctx, "crawl", lambda: pipeline.crawl(
                ctx, limit=args.limit, refresh=args.refresh, enable_steamspy=args.enable_steamspy,
                dry_run=args.dry_run, review_settings=settings), mutating=not args.dry_run)
        if args.page_size < 1:
            raise ValueError("--page-size must be positive")
        opts = _discovery_options(args)
        if cmd == "discover":
            return pipeline.run_stage(ctx, "discover", lambda: pipeline.discover(ctx, opts, dry_run=args.dry_run),
                                      mutating=not args.dry_run)
        if cmd == "run":
            return pipeline.run_all(ctx, opts, limit=args.limit, refresh=args.refresh,
                                    enable_steamspy=args.enable_steamspy, dry_run=args.dry_run)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}")
        return EXIT_USAGE
    except (pipeline.WorkspaceBusy, pipeline.WorkspaceCorrupt) as exc:
        print(f"error: {exc}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted; progress is checkpointed (continue with --resume)")
        return EXIT_INTERRUPTED
    finally:
        if ctx.transport is not None:
            ctx.transport.close()
    return EXIT_USAGE


def _appids(args: argparse.Namespace) -> list[int] | None:
    if args.appid:
        if any(a <= 0 for a in args.appid):
            print("error: --appid must be positive")
            return None
        return args.appid
    try:
        return extract.read_eligible_appids(args.input)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {args.input}: {exc}")
        return None


def run_reviews(args: argparse.Namespace, environ: Mapping[str, str], transport: SteamTransport | None = None) -> int:
    appids = _appids(args)
    if appids is None:
        return EXIT_USAGE
    try:
        settings = rv.ReviewSettings(language=args.language, purchase_type=args.purchase_type)
        key = load_api_key(environ, required=False)
    except (ValueError, InvalidCredentialError) as exc:
        print(f"error: {exc}")
        return EXIT_USAGE if not isinstance(exc, InvalidCredentialError) else EXIT_ERROR
    legacy_ids = discovery.load_legacy_appids(args.legacy_csv)
    transport = transport or SteamTransport()
    code = EXIT_OK
    with transport, rv.ReviewState(args.state) as state:
        collector = rv.ReviewCollector(state, SteamWebAPI(transport, key), settings=settings, legacy_ids=legacy_ids)
        report = None
        try:
            report = collector.run(appids, limit=args.limit, refresh=args.refresh)
            if report.stop_reason == "rate_limited":
                code = EXIT_TEMPFAIL
        except (rv.ReviewEndpointUnavailable, SteamAuthError) as exc:
            print(f"error: {exc}")
            code = EXIT_ERROR
        except SteamError as exc:
            print(f"error: {type(exc).__name__}: {exc}")
            code = EXIT_TEMPFAIL
        except KeyboardInterrupt:
            print("interrupted; progress is checkpointed")
            code = EXIT_INTERRUPTED
        counts = rv.export(state, args.out, settings)
        print(f"exported to {args.out}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        if report is not None:
            print(f"{report.stop_reason}: {report.attempted} requested, {report.pending} pending, "
                  + ", ".join(f"{k}={v}" for k, v in report.statuses.items() if v))
    return code


def run_extract(args: argparse.Namespace, environ: Mapping[str, str], transport: SteamTransport | None = None) -> int:
    appids = _appids(args)
    if appids is None:
        return EXIT_USAGE
    try:
        key = load_api_key(environ, required=False)  # tag endpoints are sent the key only if one is set
    except InvalidCredentialError as exc:
        print(f"error: {exc}")
        return EXIT_ERROR
    if args.steamspy_ttl_hours < 0:
        print("error: --steamspy-ttl-hours must be >= 0")
        return EXIT_USAGE
    legacy_ids = discovery.load_legacy_appids(args.legacy_csv)
    transport = transport or SteamTransport()
    code = EXIT_OK
    cache = spy.SteamSpyCache(args.steamspy_cache) if args.tags_source == TAGS_STEAMSPY else None
    steamspy = (spy.SteamSpyClient(transport, cache, ttl=dt.timedelta(hours=args.steamspy_ttl_hours))
                if cache is not None else None)
    with transport, extract.ExtractionState(args.state) as state:
        extractor = extract.Extractor(state, web=SteamWebAPI(transport, key), store=SteamStoreAPI(transport),
                                      legacy_ids=legacy_ids, tags_source=args.tags_source, steamspy=steamspy)
        report = None
        try:
            report = extractor.run(appids, limit=args.limit, refresh=args.refresh)
            if report.stop_reason == "rate_limited":
                code = EXIT_TEMPFAIL
        except KeyboardInterrupt:
            print("interrupted; progress is checkpointed")
            code = EXIT_INTERRUPTED
        counts = extract.export(state, args.out, rv.load_records(args.reviews_state))
        if cache is not None:
            print(f"SteamSpy: {steamspy.network_requests} network request(s); fresh cached responses were reused")
            cache.close()
        print(f"exported to {args.out}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        if report is not None:
            print(f"{report.stop_reason}: {report.attempted} fetched, {report.extracted} complete, "
                  f"{report.partial} partial, {report.failed} failed, {report.pending} pending, "
                  f"{len(report.skipped_legacy)} legacy skipped")
    return code


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         transport: SteamTransport | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ

    if args.command == "preflight":
        if args.appid <= 0:
            parser.error("--appid must be positive")
        return preflight.run(environ, live=args.live, appid=args.appid, steamspy=args.steamspy)
    if args.command == "extract":
        return run_extract(args, environ, transport)
    if args.command == "reviews":
        return run_reviews(args, environ, transport)
    return run_pipeline_command(args, environ, transport)

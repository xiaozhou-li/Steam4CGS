"""Command-line entry point.

    python -m crawler preflight [--live] [--appid N]
    python -m crawler discover  [--start DATE] [--end DATE] [--limit N] [--incremental]
                                [--resume | --restart] [--recheck-unreleased] [--state PATH] [--out DIR]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from crawler import discovery, preflight
from crawler.credentials import MissingCredentialError, InvalidCredentialError, load_api_key
from crawler.discovery_state import DiscoveryState
from crawler.eligibility import DateWindow
from crawler.steam_api import SteamStoreAPI, SteamWebAPI
from crawler.transport import SteamAuthError, SteamError, SteamTransport

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE = ROOT / "data" / "discovery" / "state.sqlite3"
DEFAULT_OUT = ROOT / "data" / "discovery"
DEFAULT_LEGACY_CSV = ROOT / "steamproducts2020.csv"

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_TEMPFAIL, EXIT_INTERRUPTED = 0, 1, 2, 75, 130


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crawler")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("preflight", help="check configuration; optionally make a few live API calls")
    p.add_argument("--live", action="store_true",
                   help=f"make at most {preflight.LIVE_MAX_REQUESTS} real requests to Steam (one app id)")
    p.add_argument("--appid", type=int, default=preflight.DEFAULT_LIVE_APPID,
                   help="app id for the live reviews/appdetails checks (default: %(default)s)")

    d = sub.add_parser("discover", help="discover games released in a date window that are not in the legacy CSV")
    d.add_argument("--start", type=_date, help=f"first release date to include (default: {discovery.DEFAULT_START})")
    d.add_argument("--end", type=_date, help="last release date to include (default: today)")
    d.add_argument("--limit", type=_non_negative, help="maximum appdetails lookups in this invocation")
    d.add_argument("--incremental", action="store_true",
                   help="only fetch catalog entries modified since the last finished run")
    mode = d.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true", help="continue the unfinished run from its checkpoint")
    mode.add_argument("--restart", action="store_true", help="abandon an unfinished run and start a new one")
    d.add_argument("--recheck-unreleased", action="store_true",
                   help="re-fetch apps previously seen as unreleased or without an exact release date")
    d.add_argument("--page-size", type=int, default=discovery.DEFAULT_PAGE_SIZE, help="GetAppList max_results")
    d.add_argument("--state", type=Path, default=DEFAULT_STATE, help="SQLite checkpoint file")
    d.add_argument("--out", type=Path, default=DEFAULT_OUT, help="directory for the exported CSV files")
    d.add_argument("--legacy-csv", type=Path, default=DEFAULT_LEGACY_CSV, help="original dataset (read-only)")
    return parser


def run_discover(args: argparse.Namespace, environ: Mapping[str, str], transport: SteamTransport | None = None) -> int:
    window = None
    if args.start or args.end:
        try:
            window = DateWindow(args.start or discovery.DEFAULT_START, args.end or dt.date.today())
        except ValueError as exc:
            print(f"error: {exc}")
            return EXIT_USAGE
    opts = discovery.DiscoveryOptions(
        window=window, limit=args.limit, incremental=args.incremental, resume=args.resume,
        restart=args.restart, recheck_unreleased=args.recheck_unreleased, page_size=args.page_size,
    )
    legacy_ids = discovery.load_legacy_appids(args.legacy_csv)
    transport = transport or SteamTransport()

    def web_factory() -> SteamWebAPI:
        # Only the catalog phase needs the key; resuming a finished catalog does not.
        return SteamWebAPI(transport, load_api_key(environ))

    with transport, DiscoveryState(args.state) as state:
        runner = discovery.Discovery(state, web_factory=web_factory, store=SteamStoreAPI(transport),
                                     legacy_ids=legacy_ids)
        report, code = None, EXIT_OK
        try:
            report = runner.run(opts)
            if report.stop_reason in (discovery.RATE_LIMITED, discovery.TOO_MANY_FAILURES):
                code = EXIT_TEMPFAIL
        except discovery.DiscoveryConfigError as exc:
            print(f"error: {exc}")
            return EXIT_USAGE
        except (MissingCredentialError, InvalidCredentialError, SteamAuthError) as exc:
            print(f"error: {exc}")
            code = EXIT_ERROR
        except SteamError as exc:
            print(f"error: {type(exc).__name__}: {exc}")
            code = EXIT_TEMPFAIL
        except KeyboardInterrupt:
            print("interrupted; progress is checkpointed")
            code = EXIT_INTERRUPTED

        run = state.get_run(report.run_id) if report else state.unfinished_run()
        if run is not None:
            counts = discovery.export(state, args.out, run, legacy_ids, report)
            print(f"exported to {args.out}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        if report is not None:
            print(f"run #{report.run_id} {report.stop_reason}: {report.lookups} lookup(s), "
                  f"{report.lookup_failures} failure(s), {report.pending} pending")
        if state.unfinished_run() is not None:
            print("continue with: python -m crawler discover --resume")
        return code


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ

    if args.command == "preflight":
        if args.appid <= 0:
            parser.error("--appid must be positive")
        return preflight.run(environ, live=args.live, appid=args.appid)
    if args.command == "discover":
        if args.page_size < 1:
            parser.error("--page-size must be positive")
        return run_discover(args, environ)
    return EXIT_USAGE

"""Command-line entry point: ``python -m crawler preflight [--live] [--appid N]``."""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence

from crawler import preflight


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m crawler")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("preflight", help="check configuration; optionally make a few live API calls")
    p.add_argument("--live", action="store_true",
                   help=f"make at most {preflight.LIVE_MAX_REQUESTS} real requests to Steam (one app id)")
    p.add_argument("--appid", type=int, default=preflight.DEFAULT_LIVE_APPID,
                   help="app id for the live reviews/appdetails checks (default: %(default)s)")
    args = parser.parse_args(argv)

    if args.command == "preflight":
        if args.appid <= 0:
            parser.error("--appid must be positive")
        return preflight.run(os.environ if environ is None else environ, live=args.live, appid=args.appid)
    return 2

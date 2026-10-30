"""Readiness checks before crawling.

Offline checks run by default and make no network requests. Live checks run
only when explicitly requested, use a hard request budget, and touch at most
one app id per endpoint.
"""

from __future__ import annotations

import platform
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import requests

from crawler.credentials import ENV_VAR, ApiKey, InvalidCredentialError, load_api_key
from crawler.steam_api import SteamStoreAPI, SteamWebAPI
from crawler.transport import RetryPolicy, SteamError, SteamTransport

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"

DEFAULT_LIVE_APPID = 620  # Portal 2: long-lived, has reviews and full store data
LIVE_MAX_REQUESTS = 9
LIVE_RETRY = RetryPolicy(max_attempts=2, backoff_base=2.0)

REQUIRED_METHODS = (
    ("IStoreService", "GetAppList", 1),
    ("IUserReviewsService", "GetAppReviews", 1),
)

LEGACY_SCRIPT = Path(__file__).resolve().parent.parent / "steamreviewcrawler.py"
_HARDCODED_KEY_RE = re.compile(r"""['"][0-9A-Fa-f]{32}['"]""")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


TransportFactory = Callable[[], SteamTransport]


def _default_transport() -> SteamTransport:
    return SteamTransport(retry=LIVE_RETRY, max_requests=LIVE_MAX_REQUESTS)


def offline_checks(environ: Mapping[str, str], legacy_script: Path = LEGACY_SCRIPT) -> tuple[list[CheckResult], ApiKey | None]:
    results = [
        CheckResult("python", OK if sys.version_info >= (3, 10) else FAIL, platform.python_version()),
        CheckResult("requests", OK, requests.__version__),
    ]
    key = None
    try:
        key = load_api_key(environ, required=False)
    except InvalidCredentialError as exc:
        results.append(CheckResult(ENV_VAR, FAIL, str(exc)))
    else:
        if key is None:
            results.append(CheckResult(
                ENV_VAR, FAIL,
                "not set; GetAppList needs it. Export it in your shell (not as a command-line argument)",
            ))
        else:
            results.append(CheckResult(ENV_VAR, OK, "set; format looks valid (value not shown)"))

    if legacy_script.exists():
        lines = [n for n, line in enumerate(legacy_script.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
                 if _HARDCODED_KEY_RE.search(line)]
        if lines:
            results.append(CheckResult(
                legacy_script.name, WARN,
                f"hard-coded API key literal on line(s) {', '.join(map(str, lines))}; "
                "revoke it at steamcommunity.com/dev/apikey and keep the file out of version control",
            ))
    return results, key


def live_checks(key: ApiKey | None, appid: int, transport: SteamTransport) -> list[CheckResult]:
    web = SteamWebAPI(transport, key)
    store = SteamStoreAPI(transport)
    results: list[CheckResult] = []

    def attempt(name: str, fn: Callable[[], CheckResult]) -> None:
        try:
            results.append(fn())
        except SteamError as exc:
            results.append(CheckResult(name, FAIL, f"{type(exc).__name__}: {exc}"))

    methods: dict = {}

    def supported() -> CheckResult:
        methods.update(web.get_supported_methods())
        scope = "visible to this key" if key else "public only (no key)"
        found = []
        for iface, name, version in REQUIRED_METHODS:
            m = methods.get((iface, name, version))
            found.append(f"{iface}/{name}/v{version}: " + (f"listed, params={list(m.parameters)}" if m else "not listed"))
        return CheckResult("GetSupportedAPIList", OK, f"{len(methods)} methods {scope}; " + "; ".join(found))

    def app_list() -> CheckResult:
        page = web.get_app_list(max_results=3)
        sample = ", ".join(f"{a.appid}" for a in page.apps)
        return CheckResult("IStoreService/GetAppList/v1", OK if page.apps else WARN,
                           f"{len(page.apps)} app(s) [{sample}], have_more_results={page.have_more_results}")

    def reviews() -> CheckResult:
        response = web.get_app_reviews(appid)
        return CheckResult("IUserReviewsService/GetAppReviews/v1", OK,
                           f"appid {appid}: response keys {sorted(response)}")

    def details() -> CheckResult:
        data = store.get_app_details(appid)
        if data is None:
            return CheckResult("store appdetails", WARN, f"appid {appid}: success=false")
        po = data.get("price_overview") or {}
        return CheckResult("store appdetails", OK,
                           f"appid {appid}: type={data.get('type')!r}, name={data.get('name')!r}, "
                           f"release_date={(data.get('release_date') or {}).get('date')!r}, "
                           f"currency={po.get('currency')!r}, {len(data)} fields")

    def review_summary() -> CheckResult:
        s = store.get_review_summary(appid)
        return CheckResult("store appreviews summary", OK,
                           f"appid {appid}: review_score_desc={s.get('review_score_desc')!r}, "
                           f"total_reviews={s.get('total_reviews')}")

    def tags() -> CheckResult:
        ids = web.get_store_item_tags(appid)
        names = web.get_tag_names()
        first = [names.get(t, f"?{t}") for t in (ids or [])[:5]]
        return CheckResult("IStoreBrowseService/GetItems + IStoreService/GetTagList", OK if ids and names else WARN,
                           f"appid {appid}: {len(ids or [])} tag id(s), {len(names)} tag name(s); first {first}")

    attempt("GetSupportedAPIList", supported)

    if key is None:
        results.append(CheckResult("IStoreService/GetAppList/v1", SKIP, f"{ENV_VAR} not set"))
    else:
        attempt("IStoreService/GetAppList/v1", app_list)

    if key is None and ("IUserReviewsService", "GetAppReviews", 1) not in methods:
        results.append(CheckResult("IUserReviewsService/GetAppReviews/v1", SKIP,
                                   f"{ENV_VAR} not set and the method is not publicly listed"))
    else:
        attempt("IUserReviewsService/GetAppReviews/v1", reviews)

    attempt("store appdetails", details)
    attempt("store appreviews summary", review_summary)
    attempt("IStoreBrowseService/GetItems + IStoreService/GetTagList", tags)
    return results


def run(
    environ: Mapping[str, str],
    *,
    live: bool = False,
    appid: int = DEFAULT_LIVE_APPID,
    transport_factory: TransportFactory = _default_transport,
    legacy_script: Path = LEGACY_SCRIPT,
    out: Callable[[str], None] = print,
) -> int:
    """Run the checks, print one line per check, and return a process exit code."""
    results, key = offline_checks(environ, legacy_script)
    secrets = [key.reveal()] if key else []
    requests_sent = 0
    if live:
        with transport_factory() as transport:
            for s in secrets:
                transport.register_secret(s)
            results += live_checks(key, appid, transport)
            requests_sent = transport.requests_sent
    else:
        results.append(CheckResult("live checks", SKIP, f"pass --live to make at most {LIVE_MAX_REQUESTS} requests"))

    out("Steam4CGS preflight")
    for r in results:
        detail = r.detail
        for s in secrets:
            detail = detail.replace(s, "***")
        out(f"[{r.status:^4}] {r.name}: {detail}")
    if live:
        out(f"live requests sent: {requests_sent}")
    return 1 if any(r.status == FAIL for r in results) else 0

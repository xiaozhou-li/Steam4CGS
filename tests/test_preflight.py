import re

import pytest
from fakes import FAKE_KEY, FakeSteam, make_transport, reply

from crawler import cli, preflight
from crawler.credentials import ENV_VAR

SUPPORTED_WITH_REVIEWS = {"apilist": {"interfaces": [
    {"name": "IStoreService", "methods": [{"name": "GetAppList", "version": 1, "httpmethod": "GET",
                                           "parameters": [{"name": "key"}]}]},
    {"name": "IUserReviewsService", "methods": [{"name": "GetAppReviews", "version": 1, "httpmethod": "GET",
                                                 "parameters": [{"name": "key"}, {"name": "appid"}]}]},
]}}
PUBLIC_ONLY = {"apilist": {"interfaces": [
    {"name": "ISteamNews", "methods": [{"name": "GetNewsForApp", "version": 2, "httpmethod": "GET", "parameters": []}]},
]}}
APP_LIST = {"response": {"apps": [{"appid": 10, "name": "CS"}, {"appid": 20, "name": "TFC"}], "have_more_results": True, "last_appid": 20}}
REVIEWS = {"response": {"review_score_desc": "Overwhelmingly Positive"}}
DETAILS = {"620": {"success": True, "data": {"type": "game", "name": "Portal 2", "steam_appid": 620}}}
SUMMARY = {"success": 1, "query_summary": {"review_score_desc": "Overwhelmingly Positive", "total_reviews": 400000}}
ITEMS = {"response": {"store_items": [{"appid": 620, "success": 1, "tagids": [128, 1664]}]}}
TAG_LIST = {"response": {"tags": [{"tagid": 128, "name": "Puzzle"}, {"tagid": 1664, "name": "Co-op"}]}}
TAIL = (reply(body=SUMMARY), reply(body=ITEMS), reply(body=TAG_LIST))
TAIL_PATHS = ["/appreviews/620", "/IStoreBrowseService/GetItems/v1/", "/IStoreService/GetTagList/v1/"]


@pytest.fixture
def no_legacy(tmp_path):
    return tmp_path / "absent.py"


def run(environ, fake=None, *, live=False, legacy_script=None, tmp_path=None):
    lines: list[str] = []
    transports = []

    def factory():
        tr = make_transport(fake if fake is not None else FakeSteam(), max_requests=preflight.LIVE_MAX_REQUESTS)
        transports.append(tr)
        return tr

    code = preflight.run(environ, live=live, appid=620, transport_factory=factory,
                         legacy_script=legacy_script, out=lines.append)
    return code, "\n".join(lines), transports


def test_offline_without_key_fails_and_makes_no_requests(no_legacy):
    fake = FakeSteam()
    code, out, transports = run({}, fake, legacy_script=no_legacy)
    assert code == 1
    assert f"[FAIL] {ENV_VAR}: not set" in out
    assert "[SKIP] live checks" in out
    assert transports == [] and fake.calls == []


def test_offline_with_key_passes_without_revealing_it(no_legacy):
    code, out, _ = run({ENV_VAR: FAKE_KEY}, legacy_script=no_legacy)
    assert code == 0
    assert f"[ OK ] {ENV_VAR}: set" in out
    assert FAKE_KEY not in out


def test_offline_malformed_key(no_legacy):
    code, out, _ = run({ENV_VAR: "not-a-key"}, legacy_script=no_legacy)
    assert code == 1 and "malformed" in out and "not-a-key" not in out


def test_warns_about_hardcoded_key_in_legacy_script(tmp_path):
    script = tmp_path / "legacy.py"
    script.write_text(f"import requests\nsteamAPIKey = '{FAKE_KEY}'\n")
    _, out, _ = run({ENV_VAR: FAKE_KEY}, legacy_script=script)
    assert "[WARN] legacy.py: hard-coded API key literal on line(s) 2" in out
    assert FAKE_KEY not in out


def test_live_with_key_runs_all_checks_within_budget(no_legacy):
    fake = FakeSteam(reply(body=SUPPORTED_WITH_REVIEWS), reply(body=APP_LIST), reply(body=REVIEWS), reply(body=DETAILS),
                     *TAIL)
    code, out, (tr,) = run({ENV_VAR: FAKE_KEY}, fake, live=True, legacy_script=no_legacy)
    assert code == 0, out
    assert [c.path for c in fake.calls] == [
        "/ISteamWebAPIUtil/GetSupportedAPIList/v1/",
        "/IStoreService/GetAppList/v1/",
        "/IUserReviewsService/GetAppReviews/v1/",
        "/api/appdetails",
        *TAIL_PATHS,
    ]
    assert fake.calls[1].params["max_results"] == "3"
    assert "IUserReviewsService/GetAppReviews/v1: listed, params=['key', 'appid']" in out
    assert "[ OK ] IStoreService/GetAppList/v1: 2 app(s) [10, 20]" in out
    assert "name='Portal 2'" in out
    assert "review_score_desc='Overwhelmingly Positive'" in out
    assert "2 tag id(s), 2 tag name(s); first ['Puzzle', 'Co-op']" in out
    assert "live requests sent: 7" in out
    assert tr.requests_sent <= preflight.LIVE_MAX_REQUESTS
    assert FAKE_KEY not in out


def test_live_without_key_only_uses_public_endpoints(no_legacy):
    fake = FakeSteam(reply(body=PUBLIC_ONLY), reply(body=DETAILS), *TAIL)
    code, out, _ = run({}, fake, live=True, legacy_script=no_legacy)
    assert code == 1  # the missing key is still a failure
    assert [c.path for c in fake.calls] == ["/ISteamWebAPIUtil/GetSupportedAPIList/v1/", "/api/appdetails", *TAIL_PATHS]
    assert all("key" not in c.params for c in fake.calls)
    assert "[SKIP] IStoreService/GetAppList/v1" in out
    assert "[SKIP] IUserReviewsService/GetAppReviews/v1" in out
    assert "public only (no key)" in out
    assert "[ OK ] store appdetails" in out


def test_live_reports_endpoint_failures_without_leaking_key(no_legacy):
    fake = FakeSteam(
        reply(body=SUPPORTED_WITH_REVIEWS),
        reply(403, text=f"denied {FAKE_KEY}"),
        reply(404, text="Not Found"),
        reply(body={"620": {"success": False}}),
        reply(500, text="boom"), reply(500, text="boom"), reply(500, text="boom"),
        reply(body={"response": {"store_items": []}}), reply(body=TAG_LIST),
    )
    code, out, _ = run({ENV_VAR: FAKE_KEY}, fake, live=True, legacy_script=no_legacy)
    assert code == 1
    assert "[FAIL] IStoreService/GetAppList/v1: SteamAuthError" in out
    assert "[FAIL] IUserReviewsService/GetAppReviews/v1: SteamHTTPError" in out
    assert "[WARN] store appdetails: appid 620: success=false" in out
    assert "[FAIL] store appreviews summary: SteamHTTPError" in out
    assert "[WARN] IStoreBrowseService/GetItems + IStoreService/GetTagList: appid 620: 0 tag id(s)" in out
    assert FAKE_KEY not in out


def test_cli_preflight_offline_never_echoes_any_key(capsys):
    # Runs against the real repository, including the legacy script if present.
    assert cli.main(["preflight"], environ={ENV_VAR: FAKE_KEY}) == 0
    out = capsys.readouterr().out
    assert "[SKIP] live checks" in out
    assert not re.search(r"[0-9A-Fa-f]{32}", out)
    if preflight.LEGACY_SCRIPT.exists():
        assert f"[WARN] {preflight.LEGACY_SCRIPT.name}: hard-coded API key literal" in out


def test_cli_rejects_bad_appid(capsys):
    with pytest.raises(SystemExit):
        cli.main(["preflight", "--appid", "0"], environ={})

import email.utils
import logging

import pytest
import requests
from fakes import FAKE_KEY, FakeClock, FakeSteam, make_transport, reply

from crawler import transport as t
from crawler.transport import (
    RateLimiter,
    RequestBudgetExceeded,
    RetryPolicy,
    SteamAuthError,
    SteamHTTPError,
    SteamRateLimitError,
    SteamResponseError,
    SteamTransportError,
)

URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
STORE_URL = "https://store.steampowered.com/api/appdetails"


def test_success_sends_timeout_user_agent_and_params():
    fake = FakeSteam(reply(body={"ok": 1}))
    tr = make_transport(fake, timeout=(2.0, 9.0))
    assert tr.get_json(URL, {"max_results": 3}) == {"ok": 1}
    (call,) = fake.calls
    assert call.timeout == (2.0, 9.0)
    assert call.params == {"max_results": "3"}
    assert call.request.headers["User-Agent"].startswith("steam4cgs-crawler/")
    assert tr.requests_sent == 1


def test_default_timeout_is_finite():
    assert all(0 < x < 120 for x in t.DEFAULT_TIMEOUT)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_retries_server_errors_with_exponential_backoff(status):
    clock = FakeClock()
    fake = FakeSteam(reply(status, {}), reply(status, {}), reply(body={"ok": 1}))
    tr = make_transport(fake, clock, retry=RetryPolicy(max_attempts=3, backoff_base=2.0))
    assert tr.get_json(URL) == {"ok": 1}
    assert len(fake.calls) == 3
    # rand() == 0 -> half of the exponential ceiling: 2*2^0/2, 2*2^1/2
    assert clock.sleeps == [1.0, 2.0]


def test_backoff_is_capped_and_jittered():
    policy = RetryPolicy(backoff_base=1.0, backoff_max=10.0)
    assert policy.backoff(1, lambda: 0.0) == 0.5
    assert policy.backoff(1, lambda: 1.0) == 1.0
    assert policy.backoff(20, lambda: 1.0) == 10.0


def test_gives_up_after_max_attempts():
    fake = FakeSteam(*[reply(503, {}) for _ in range(3)])
    with pytest.raises(SteamHTTPError) as info:
        make_transport(fake).get_json(URL)
    assert info.value.status == 503
    assert "after 3 attempt(s)" in str(info.value)
    assert len(fake.calls) == 3


def test_429_honours_retry_after_seconds():
    clock = FakeClock()
    fake = FakeSteam(reply(429, {}, headers={"Retry-After": "7"}), reply(body=[1]))
    assert make_transport(fake, clock).get_json(URL) == [1]
    assert clock.sleeps == [7.0]


def test_429_honours_retry_after_http_date():
    clock = FakeClock(now=1_700_000_000.0)
    date = email.utils.formatdate(clock.now + 12, usegmt=True)
    fake = FakeSteam(reply(429, {}, headers={"Retry-After": date}), reply(body=[1]))
    make_transport(fake, clock).get_json(URL)
    assert clock.sleeps == [12.0]


def test_429_exhausted_raises_rate_limit_error():
    fake = FakeSteam(*[reply(429, {}, headers={"Retry-After": "1"}) for _ in range(3)])
    with pytest.raises(SteamRateLimitError) as info:
        make_transport(fake).get_json(URL)
    assert info.value.retry_after == 1.0


def test_excessive_retry_after_is_not_waited_out():
    clock = FakeClock()
    fake = FakeSteam(reply(429, {}, headers={"Retry-After": "3600"}))
    with pytest.raises(SteamRateLimitError):
        make_transport(fake, clock).get_json(URL)
    assert clock.sleeps == [] and len(fake.calls) == 1


@pytest.mark.parametrize("value, expected", [("5", 5.0), (" 0 ", 0.0), ("soon", None), ("", None), (None, None)])
def test_parse_retry_after(value, expected):
    assert t.parse_retry_after(value, lambda: 0.0) == expected


@pytest.mark.parametrize("status", [400, 404, 410])
def test_client_errors_are_not_retried(status):
    fake = FakeSteam(reply(status, {}))
    with pytest.raises(SteamHTTPError) as info:
        make_transport(fake).get_json(URL)
    assert info.value.status == status and len(fake.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_on_authenticated_calls(status):
    fake = FakeSteam(reply(status, text="<html>Forbidden</html>"))
    with pytest.raises(SteamAuthError):
        make_transport(fake).get_json(URL, authenticated=True)


def test_403_without_auth_is_plain_http_error():
    fake = FakeSteam(reply(403, text="denied"))
    with pytest.raises(SteamHTTPError) as info:
        make_transport(fake).get_json(STORE_URL)
    assert not isinstance(info.value, SteamAuthError)


def test_redirects_are_not_followed():
    fake = FakeSteam(reply(302, text="", headers={"Location": "https://evil.example/steal"}))
    with pytest.raises(SteamHTTPError) as info:
        make_transport(fake).get_json(URL, {"key": FAKE_KEY})
    assert info.value.status == 302 and len(fake.calls) == 1


@pytest.mark.parametrize("exc", [requests.ConnectionError, requests.Timeout, requests.exceptions.ReadTimeout])
def test_network_errors_are_retried_then_wrapped(exc):
    leaked = f"Max retries exceeded with url: /IStoreService/GetAppList/v1/?key={FAKE_KEY}&max_results=3"
    clock = FakeClock()
    fake = FakeSteam(exc(leaked), exc(leaked), exc(leaked))
    tr = make_transport(fake, clock)
    tr.register_secret(FAKE_KEY)
    with pytest.raises(SteamTransportError) as info:
        tr.get_json(URL, {"key": FAKE_KEY})
    assert len(fake.calls) == 3 and len(clock.sleeps) == 2
    assert FAKE_KEY not in str(info.value)
    assert "key=***" in str(info.value)
    # The original exception (whose message has the key) is not chained.
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_network_error_then_success():
    fake = FakeSteam(requests.ConnectionError("reset"), reply(body={"ok": True}))
    assert make_transport(fake).get_json(URL) == {"ok": True}


def test_non_json_body_raises_response_error():
    fake = FakeSteam(reply(200, text="<html>maintenance</html>", headers={"Content-Type": "text/html"}))
    with pytest.raises(SteamResponseError, match="non-JSON"):
        make_transport(fake).get_json(URL)


def test_http_error_message_redacts_key_from_body():
    fake = FakeSteam(reply(403, text=f"Bad key {FAKE_KEY}"))
    tr = make_transport(fake)
    tr.register_secret(FAKE_KEY)
    with pytest.raises(SteamAuthError) as info:
        tr.get_json(URL, {"key": FAKE_KEY}, authenticated=True)
    assert FAKE_KEY not in str(info.value) and FAKE_KEY not in repr(info.value)
    assert info.value.endpoint == "api.steampowered.com/IStoreService/GetAppList/v1/"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.steampowered.com/IStoreService/GetAppList/v1/",     # not https
        "https://partner.steam-api.com/IStoreService/GetAppList/v1/",   # not allowlisted
        "https://api.steampowered.com.evil.example/x",
        f"https://api.steampowered.com/x/?key={FAKE_KEY}",              # query in URL
    ],
)
def test_url_policy_rejects_before_sending(url):
    fake = FakeSteam()
    with pytest.raises(ValueError):
        make_transport(fake).get_json(url)
    assert fake.calls == []


def test_request_budget_counts_retries():
    fake = FakeSteam(reply(503, {}), reply(503, {}), reply(body=1))
    tr = make_transport(fake, max_requests=2)
    with pytest.raises(RequestBudgetExceeded):
        tr.get_json(URL)
    assert len(fake.calls) == 2


def test_rate_limiter_spaces_requests_per_host():
    clock = FakeClock()
    fake = FakeSteam(*[reply(body=i) for i in range(4)])
    tr = make_transport(fake, clock, min_intervals={"api.steampowered.com": 1.0, "store.steampowered.com": 1.5})
    tr.get_json(URL)
    tr.get_json(STORE_URL)   # different host: no wait
    tr.get_json(URL)         # waits for the api host interval
    tr.get_json(STORE_URL)
    assert clock.sleeps == [1.0, 0.5]


def test_rate_limiter_penalty_delays_next_request():
    clock = FakeClock()
    limiter = RateLimiter(1.0, clock=clock.time, sleep=clock.sleep)
    limiter.acquire()
    limiter.penalize(10.0)
    limiter.acquire()
    assert clock.sleeps == [10.0]


def test_rate_limiter_rejects_negative_interval():
    with pytest.raises(ValueError):
        RateLimiter(-1)


def test_urllib3_debug_log_is_redacted(caplog):
    logger = logging.getLogger("urllib3.connectionpool")
    with caplog.at_level(logging.DEBUG, logger="urllib3.connectionpool"):
        logger.debug('%s "GET %s HTTP/1.1" %s', "https://api.steampowered.com:443",
                     f"/IStoreService/GetAppList/v1/?key={FAKE_KEY}&max_results=3", 200)
    assert FAKE_KEY not in caplog.text
    assert "key=***&max_results=3" in caplog.text


def test_redact_handles_secrets_and_key_params():
    assert t.redact(f"x {FAKE_KEY} y", [FAKE_KEY]) == "x *** y"
    assert t.redact("a?key=SECRET&b=1") == "a?key=***&b=1"
    assert t.redact("monkey=banana") == "monkey=banana"

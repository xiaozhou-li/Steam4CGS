"""Test doubles: a scripted requests adapter and a fake clock. No sockets are opened."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from crawler.transport import RetryPolicy, SteamTransport

# Built at runtime so no 32-hex literal appears in the source.
FAKE_KEY = "AB" * 16


def reply(status: int = 200, body=None, *, headers=None, text: str | None = None):
    content = text.encode() if text is not None else json.dumps(body).encode()
    return status, content, headers or {"Content-Type": "application/json"}


@dataclass
class Call:
    request: requests.PreparedRequest
    timeout: object

    @property
    def url(self) -> str:
        return self.request.url

    @property
    def path(self) -> str:
        return urlsplit(self.request.url).path

    @property
    def host(self) -> str:
        return urlsplit(self.request.url).hostname

    @property
    def params(self) -> dict[str, str]:
        return {k: v[-1] for k, v in parse_qs(urlsplit(self.request.url).query).items()}


class FakeSteam(BaseAdapter):
    """Returns queued replies in order; raises queued exceptions."""

    def __init__(self, *replies):
        super().__init__()
        self.replies = list(replies)
        self.calls: list[Call] = []

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        self.calls.append(Call(request, timeout))
        if not self.replies:
            raise AssertionError(f"unexpected request to {urlsplit(request.url).path}")
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(request)
        status, content, headers = item
        response = requests.Response()
        response.status_code = status
        response.reason = {200: "OK", 403: "Forbidden", 404: "Not Found", 429: "Too Many Requests"}.get(status, "")
        response._content = content
        response._content_consumed = True
        response.headers = CaseInsensitiveDict(headers)
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass


@dataclass
class FakeClock:
    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


def make_transport(fake: FakeSteam, clock: FakeClock | None = None, **kwargs) -> SteamTransport:
    clock = clock or FakeClock()
    session = requests.Session()
    session.mount("https://", fake)
    session.mount("http://", fake)
    kwargs.setdefault("retry", RetryPolicy(max_attempts=3, backoff_base=1.0))
    kwargs.setdefault("min_intervals", {"api.steampowered.com": 0.0, "store.steampowered.com": 0.0,
                                        "steamspy.com": 0.0})
    return SteamTransport(
        session=session, sleep=clock.sleep, clock=clock.time, wall_clock=clock.time, rand=lambda: 0.0, **kwargs
    )

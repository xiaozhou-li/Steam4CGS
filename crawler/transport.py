"""HTTP transport for Steam endpoints: timeouts, retries, rate limiting, redaction.

All crawler network I/O goes through :class:`SteamTransport`. It only speaks
HTTPS to an allowlist of hosts (Steam, plus the optional third-party SteamSpy)
and never follows redirects, so a query-string API key cannot be forwarded to
another host (the key is only ever added by SteamWebAPI, for
api.steampowered.com). API keys are scrubbed from every error message it
raises and from urllib3's debug log.
"""

from __future__ import annotations

import email.utils
import logging
import random
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

USER_AGENT = f"steam4cgs-crawler/0.1 python-requests/{requests.__version__}"
ALLOWED_HOSTS = frozenset({"api.steampowered.com", "store.steampowered.com", "steamspy.com"})
DEFAULT_TIMEOUT = (5.0, 30.0)  # (connect, read) seconds

# Minimum seconds between requests to each host. 1.0 s keeps a key below the
# Web API's 100,000 calls/day; Store appdetails has no published limit and is
# commonly throttled near 200 requests per 5 minutes. SteamSpy documents
# 1 request per second for appdetails; 1.1 s leaves a margin.
DEFAULT_MIN_INTERVALS: Mapping[str, float] = {
    "api.steampowered.com": 1.0,
    "store.steampowered.com": 1.5,
    "steamspy.com": 1.1,
}

_KEY_PARAM_RE = re.compile(r"(?i)\b(key=)[^&\s'\"]+")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace known secrets and any ``key=...`` query value with ``***``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return _KEY_PARAM_RE.sub(r"\1***", text)


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact(message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


# urllib3 logs full request URLs (including ?key=...) at DEBUG level.
for _name in ("urllib3.connectionpool", __name__):
    logging.getLogger(_name).addFilter(_RedactingFilter())


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class SteamError(Exception):
    """Base class for crawler API errors. Messages never contain API keys."""


class SteamTransportError(SteamError):
    """Network failure (DNS, connect, TLS, timeout) after all retries."""


class SteamResponseError(SteamError):
    """The server answered, but not with the JSON shape we expected."""


class RequestBudgetExceeded(SteamError):
    """The transport's ``max_requests`` limit would be exceeded."""


class SteamHTTPError(SteamError):
    def __init__(self, message: str, *, status: int, endpoint: str):
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint


class SteamAuthError(SteamHTTPError):
    """401/403 on an authenticated call: missing, invalid or unauthorized key."""


class SteamRateLimitError(SteamHTTPError):
    def __init__(self, message: str, *, status: int, endpoint: str, retry_after: float | None):
        super().__init__(message, status=status, endpoint=endpoint)
        self.retry_after = retry_after


# --------------------------------------------------------------------------
# Retry and rate limiting
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    # A Retry-After longer than this is not waited out; the error is raised instead.
    max_retry_after: float = 300.0
    retry_statuses: frozenset[int] = frozenset({429, 500, 502, 503, 504})

    def backoff(self, attempt: int, rand: Callable[[], float] = random.random) -> float:
        """Delay after the given failed 1-based attempt: exponential with jitter."""
        ceiling = min(self.backoff_max, self.backoff_base * 2 ** (attempt - 1))
        return ceiling / 2 + rand() * ceiling / 2


def parse_retry_after(value: str | None, now: Callable[[], float] = time.time) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - now())


class RateLimiter:
    """Enforces a minimum interval between requests. Thread-safe."""

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if min_interval < 0:
            raise ValueError("min_interval must be >= 0")
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._next = float("-inf")
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            if self._next > now:
                self._sleep(self._next - now)
                now = self._clock()
            self._next = max(now, self._next) + self.min_interval

    def penalize(self, seconds: float) -> None:
        """Hold every caller back for at least ``seconds`` from now."""
        with self._lock:
            self._next = max(self._next, self._clock() + seconds)


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


class SteamTransport:
    """Rate-limited, retrying JSON-over-HTTPS client for Steam hosts."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
        retry: RetryPolicy = RetryPolicy(),
        min_intervals: Mapping[str, float] = DEFAULT_MIN_INTERVALS,
        max_requests: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        rand: Callable[[], float] = random.random,
    ):
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._timeout = timeout
        self._retry = retry
        self._limiters = {
            host: RateLimiter(min_intervals.get(host, max(DEFAULT_MIN_INTERVALS.values())), clock=clock, sleep=sleep)
            for host in ALLOWED_HOSTS
        }
        self._max_requests = max_requests
        self._wall_clock = wall_clock
        self._rand = rand
        self._secrets: set[str] = set()
        self._count_lock = threading.Lock()
        self.requests_sent = 0

    def register_secret(self, secret: str) -> None:
        """Add a value that must be scrubbed from every error message."""
        if secret:
            self._secrets.add(secret)

    def redact(self, text: str) -> str:
        return redact(text, self._secrets)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> SteamTransport:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_json(self, url: str, params: Mapping[str, Any] | None = None, *, authenticated: bool = False) -> Any:
        """GET ``url`` and return the decoded JSON body.

        Retries connection errors, timeouts and ``retry.retry_statuses`` with
        backoff (honouring Retry-After). Raises a :class:`SteamError` subclass.
        """
        host, endpoint = self._check_url(url)
        limiter = self._limiters[host]
        attempt = 0
        while True:
            attempt += 1
            self._consume_budget(endpoint)
            limiter.acquire()
            try:
                response = self._session.get(url, params=params, timeout=self._timeout, allow_redirects=False)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt < self._retry.max_attempts:
                    delay = self._retry.backoff(attempt, self._rand)
                    log.warning("GET %s: %s; retrying in %.1fs", endpoint, type(exc).__name__, delay)
                    limiter.penalize(delay)
                    continue
                raise SteamTransportError(
                    self.redact(f"GET {endpoint} failed after {attempt} attempt(s): {type(exc).__name__}: {exc}")
                ) from None
            except requests.RequestException as exc:
                raise SteamTransportError(self.redact(f"GET {endpoint} failed: {type(exc).__name__}: {exc}")) from None

            with response:
                status = response.status_code
                log.debug("GET %s -> %s (attempt %d)", endpoint, status, attempt)
                if 200 <= status < 300:
                    return self._decode(response, endpoint)

                if status in self._retry.retry_statuses:
                    retry_after = parse_retry_after(response.headers.get("Retry-After"), self._wall_clock)
                    waitable = retry_after is None or retry_after <= self._retry.max_retry_after
                    if attempt < self._retry.max_attempts and waitable:
                        delay = max(retry_after or 0.0, self._retry.backoff(attempt, self._rand))
                        log.warning("GET %s -> HTTP %d; retrying in %.1fs", endpoint, status, delay)
                        limiter.penalize(delay)
                        continue
                    message = self._http_message(endpoint, response, attempt)
                    if status == 429:
                        raise SteamRateLimitError(message, status=status, endpoint=endpoint, retry_after=retry_after)
                    raise SteamHTTPError(message, status=status, endpoint=endpoint)

                message = self._http_message(endpoint, response, attempt)
                if authenticated and status in (401, 403):
                    raise SteamAuthError(message, status=status, endpoint=endpoint)
                raise SteamHTTPError(message, status=status, endpoint=endpoint)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _check_url(url: str) -> tuple[str, str]:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise ValueError(f"only https URLs are allowed, got scheme {parts.scheme!r}")
        if parts.hostname not in ALLOWED_HOSTS:
            raise ValueError(f"host {parts.hostname!r} is not in the Steam allowlist {sorted(ALLOWED_HOSTS)}")
        if parts.query or parts.fragment:
            raise ValueError("pass query parameters via `params`, not in the URL")
        return parts.hostname, f"{parts.hostname}{parts.path}"

    def _consume_budget(self, endpoint: str) -> None:
        with self._count_lock:
            if self._max_requests is not None and self.requests_sent >= self._max_requests:
                raise RequestBudgetExceeded(
                    f"request budget of {self._max_requests} exhausted before GET {endpoint}"
                )
            self.requests_sent += 1

    def _decode(self, response: requests.Response, endpoint: str) -> Any:
        try:
            return response.json()
        except ValueError:
            content_type = response.headers.get("Content-Type", "unknown")
            raise SteamResponseError(
                f"GET {endpoint} returned non-JSON content (Content-Type: {content_type})"
            ) from None

    def _http_message(self, endpoint: str, response: requests.Response, attempts: int) -> str:
        reason = response.reason or ""
        snippet = " ".join(response.text[:200].split())
        tail = f": {snippet}" if snippet else ""
        return self.redact(f"GET {endpoint} -> HTTP {response.status_code} {reason} after {attempts} attempt(s){tail}")

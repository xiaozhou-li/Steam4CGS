"""Optional SteamSpy integration for community tags.

SteamSpy (https://steamspy.com/api.php) is a third-party, unofficial service.
Its per-game ``appdetails`` response has ``tags``: a JSON object mapping tag
name to vote count. SteamSpy documents a rate limit of 1 request per second
for this endpoint and refreshes its data about once a day, so responses are
cached for 24 hours by default and the transport spaces requests >= 1.1 s.
The Steam Web API key is never sent here.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crawler.discovery_state import iso
from crawler.steam_api import public_url
from crawler.transport import SteamResponseError, SteamTransport

STEAMSPY_API = "https://steamspy.com/api.php"
DEFAULT_TTL = dt.timedelta(hours=24)


class SteamSpyFormatError(ValueError):
    """The SteamSpy response does not have the documented shape."""


def appdetails_request(appid: int) -> tuple[str, dict]:
    return STEAMSPY_API, {"request": "appdetails", "appid": appid}


def tag_sort_key(item: tuple[str, int]) -> tuple[int, str, str]:
    """Most votes first; ties by case-insensitive then exact name, so the order is total."""
    name, votes = item
    return (-votes, name.casefold(), name)


def parse_tags(appid: int, data: Any) -> list[tuple[str, int]] | None:
    """All (tag, votes) pairs in deterministic order.

    Returns ``None`` when SteamSpy has no record for the app (``name`` is null),
    ``[]`` when the record has no tags, and raises :class:`SteamSpyFormatError`
    for anything that does not match the documented shape.
    """
    if not isinstance(data, Mapping):
        raise SteamSpyFormatError(f"expected a JSON object, got {type(data).__name__}")
    answered = data.get("appid")
    if str(answered) != str(appid):
        raise SteamSpyFormatError(f"response is for appid {answered!r}, not {appid}")
    if not data.get("name"):
        return None
    tags = data.get("tags")
    if tags is None or tags == [] or tags == {}:  # PHP encodes an empty object as []
        return []
    if not isinstance(tags, Mapping):
        raise SteamSpyFormatError(f"'tags' must be an object of name -> votes, got {type(tags).__name__}")
    out: dict[str, int] = {}
    for name, votes in tags.items():
        clean = name.strip() if isinstance(name, str) else ""
        if not clean or "\n" in clean or "\r" in clean:
            raise SteamSpyFormatError(f"invalid tag name {name!r}")
        if isinstance(votes, bool) or not isinstance(votes, int) or votes < 0:
            raise SteamSpyFormatError(f"tag {clean!r} has invalid vote count {votes!r}")
        if clean in out:
            raise SteamSpyFormatError(f"duplicate tag {clean!r}")
        out[clean] = votes
    return sorted(out.items(), key=tag_sort_key)


class SteamSpyCache:
    """Raw SteamSpy appdetails bodies with their original retrieval time."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        with self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS appdetails (
                appid INTEGER PRIMARY KEY, body_json TEXT NOT NULL, retrieved_at TEXT NOT NULL)""")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> SteamSpyCache:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, appid: int) -> tuple[Any, str] | None:
        row = self._db.execute("SELECT body_json, retrieved_at FROM appdetails WHERE appid = ?", (appid,)).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def put(self, appid: int, body: Any, retrieved_at: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO appdetails VALUES (?, ?, ?) ON CONFLICT(appid) DO UPDATE SET "
                "body_json = excluded.body_json, retrieved_at = excluded.retrieved_at",
                (appid, json.dumps(body, ensure_ascii=False), retrieved_at))


@dataclass(frozen=True)
class SteamSpyResponse:
    data: Any
    url: str
    retrieved_at: str
    from_cache: bool


class SteamSpyClient:
    def __init__(
        self,
        transport: SteamTransport,
        cache: SteamSpyCache | None = None,
        *,
        ttl: dt.timedelta = DEFAULT_TTL,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
    ):
        self._transport = transport
        self._cache = cache
        self._ttl = ttl
        self._now = now
        self.network_requests = 0

    def appdetails(self, appid: int) -> SteamSpyResponse:
        """SteamSpy appdetails for one app, from the cache when fresher than the TTL."""
        url, params = appdetails_request(appid)
        display = public_url(url, params)
        if self._cache is not None:
            cached = self._cache.get(appid)
            if cached is not None:
                body, retrieved_at = cached
                age = self._now() - dt.datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
                if dt.timedelta(0) <= age < self._ttl:
                    return SteamSpyResponse(body, display, retrieved_at, from_cache=True)
        self.network_requests += 1
        body = self._transport.get_json(url, params)
        if not isinstance(body, dict):
            raise SteamResponseError(f"SteamSpy appdetails returned {type(body).__name__}, expected an object")
        retrieved_at = iso(self._now())
        if self._cache is not None:
            self._cache.put(appid, body, retrieved_at)
        return SteamSpyResponse(body, display, retrieved_at, from_cache=False)

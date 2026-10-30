"""SQLite response cache keyed by request, storing each body with its original retrieval time.

A cache hit returns the time the response was first fetched, so provenance
stays truthful about data age. Only successful, well-formed responses are
stored; failures are never cached.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


def cache_key(url: str, params: Mapping[str, Any]) -> str:
    """Canonical request key; an API key parameter is never part of it."""
    return f"{url}?{urlencode(sorted((k, str(v)) for k, v in params.items() if k != 'key'))}"


def _parse_time(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


class ResponseCache:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._db:
            self._db.execute("""CREATE TABLE IF NOT EXISTS responses (
                key TEXT PRIMARY KEY, body_json TEXT NOT NULL, retrieved_at TEXT NOT NULL)""")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> ResponseCache:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, key: str, *, now: dt.datetime, ttl: dt.timedelta) -> tuple[Any, str] | None:
        """``(body, retrieved_at)`` when an entry younger than ``ttl`` exists."""
        row = self._db.execute("SELECT body_json, retrieved_at FROM responses WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        age = now - _parse_time(row[1])
        if not dt.timedelta(0) <= age < ttl:
            return None
        return json.loads(row[0]), row[1]

    def put(self, key: str, body: Any, retrieved_at: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO responses VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "body_json = excluded.body_json, retrieved_at = excluded.retrieved_at",
                (key, json.dumps(body, ensure_ascii=False), retrieved_at))

    def __len__(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM responses").fetchone()[0]

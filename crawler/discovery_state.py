"""SQLite-backed discovery state: runs, catalog checkpoints, fetched facts, failures.

Every catalog page and every appdetails result is committed in its own
transaction, so an interrupted run loses at most the request in flight.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from crawler.eligibility import AppFacts, DateWindow
from crawler.steam_api import AppListEntry

SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    run_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    status             TEXT NOT NULL CHECK (status IN ('running', 'finished', 'abandoned')),
    mode               TEXT NOT NULL CHECK (mode IN ('full', 'incremental', 'seed')),
    window_start       TEXT NOT NULL,
    window_end         TEXT NOT NULL,
    recheck_unreleased INTEGER NOT NULL,
    if_modified_since  INTEGER,
    catalog_started_at INTEGER NOT NULL,
    catalog_cursor     INTEGER,
    catalog_pages      INTEGER NOT NULL DEFAULT 0,
    catalog_complete   INTEGER NOT NULL DEFAULT 0,
    started_at         TEXT NOT NULL,
    finished_at        TEXT
);
CREATE TABLE IF NOT EXISTS discovered (
    appid               INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    last_modified       INTEGER,
    price_change_number INTEGER,
    first_seen_run      INTEGER NOT NULL,
    last_seen_run       INTEGER NOT NULL,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facts (
    appid                INTEGER PRIMARY KEY,
    facts_json           TEXT NOT NULL,
    source_last_modified INTEGER,
    recheckable          INTEGER NOT NULL,
    fetched_at           TEXT NOT NULL,
    fetched_run          INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS failures (
    appid            INTEGER PRIMARY KEY,
    error            TEXT NOT NULL,
    detail           TEXT NOT NULL,
    attempts         INTEGER NOT NULL,
    last_attempt_at  TEXT NOT NULL,
    last_attempt_run INTEGER NOT NULL
);
"""

# An app needs an appdetails lookup in this run when it has no facts yet,
# its catalog last_modified moved past the facts we hold, it failed in an
# earlier run, or (with --recheck-unreleased) its facts were unreleased/imprecise
# and fetched in an earlier run.
_PENDING_SQL = """
SELECT d.appid, d.last_modified
FROM discovered d
LEFT JOIN facts f ON f.appid = d.appid
LEFT JOIN failures x ON x.appid = d.appid
WHERE (x.appid IS NOT NULL AND x.last_attempt_run <> :run)
   OR (x.appid IS NULL AND (
          f.appid IS NULL
       OR (d.last_modified IS NOT NULL
           AND (f.source_last_modified IS NULL OR d.last_modified > f.source_last_modified))
       OR (:recheck AND f.recheckable = 1 AND f.fetched_run <> :run)))
ORDER BY d.appid DESC
"""


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Run:
    run_id: int
    status: str
    mode: str
    window: DateWindow
    recheck_unreleased: bool
    if_modified_since: int | None
    catalog_started_at: int
    catalog_cursor: int | None
    catalog_pages: int
    catalog_complete: bool
    started_at: str
    finished_at: str | None


@dataclass(frozen=True)
class DiscoveredApp:
    appid: int
    name: str
    last_modified: int | None
    price_change_number: int | None
    first_seen_at: str
    last_seen_at: str


@dataclass(frozen=True)
class StoredFacts:
    facts: AppFacts
    fetched_at: str


@dataclass(frozen=True)
class Failure:
    appid: int
    error: str
    detail: str
    attempts: int
    last_attempt_at: str


class DiscoveryState:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        with self._db:
            self._db.executescript(_SCHEMA)
            row = self._db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                self._db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            elif row["value"] != SCHEMA_VERSION:
                raise RuntimeError(f"{self.path}: unsupported state schema version {row['value']!r}")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> DiscoveryState:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- runs ------------------------------------------------------------------

    @staticmethod
    def _run(row: sqlite3.Row | None) -> Run | None:
        if row is None:
            return None
        return Run(
            run_id=row["run_id"],
            status=row["status"],
            mode=row["mode"],
            window=DateWindow(dt.date.fromisoformat(row["window_start"]), dt.date.fromisoformat(row["window_end"])),
            recheck_unreleased=bool(row["recheck_unreleased"]),
            if_modified_since=row["if_modified_since"],
            catalog_started_at=row["catalog_started_at"],
            catalog_cursor=row["catalog_cursor"],
            catalog_pages=row["catalog_pages"],
            catalog_complete=bool(row["catalog_complete"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def get_run(self, run_id: int) -> Run:
        run = self._run(self._db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())
        if run is None:
            raise KeyError(run_id)
        return run

    def unfinished_run(self) -> Run | None:
        return self._run(self._db.execute(
            "SELECT * FROM runs WHERE status = 'running' ORDER BY run_id DESC LIMIT 1").fetchone())

    def last_finished_run(self) -> Run | None:
        """The last finished catalog run (seed runs never set an incremental starting point)."""
        return self._run(self._db.execute(
            "SELECT * FROM runs WHERE status = 'finished' AND mode != 'seed' ORDER BY run_id DESC LIMIT 1").fetchone())

    def latest_run(self) -> Run | None:
        """The most recent run that was not abandoned (its window defines current eligibility)."""
        return self._run(self._db.execute(
            "SELECT * FROM runs WHERE status != 'abandoned' ORDER BY run_id DESC LIMIT 1").fetchone())

    def start_run(self, *, window: DateWindow, mode: str, if_modified_since: int | None,
                  recheck_unreleased: bool, now: dt.datetime) -> Run:
        with self._db:
            cur = self._db.execute(
                """INSERT INTO runs (status, mode, window_start, window_end, recheck_unreleased,
                                     if_modified_since, catalog_started_at, started_at)
                   VALUES ('running', ?, ?, ?, ?, ?, ?, ?)""",
                (mode, window.start.isoformat(), window.end.isoformat(), int(recheck_unreleased),
                 if_modified_since, int(now.timestamp()), iso(now)),
            )
        return self.get_run(cur.lastrowid)

    def set_run_status(self, run_id: int, status: str, now: dt.datetime) -> None:
        with self._db:
            self._db.execute("UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?", (status, iso(now), run_id))

    # -- catalog -----------------------------------------------------------------

    def save_catalog_page(self, run_id: int, apps: Iterable[AppListEntry], *, cursor: int | None,
                          complete: bool, now: dt.datetime) -> None:
        """Upsert one page of apps and advance the run's cursor atomically."""
        stamp = iso(now)
        with self._db:
            self._db.executemany(
                """INSERT INTO discovered (appid, name, last_modified, price_change_number,
                                           first_seen_run, last_seen_run, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(appid) DO UPDATE SET
                       name = excluded.name,
                       last_modified = excluded.last_modified,
                       price_change_number = excluded.price_change_number,
                       last_seen_run = excluded.last_seen_run,
                       last_seen_at = excluded.last_seen_at""",
                [(a.appid, a.name, a.last_modified, a.price_change_number, run_id, run_id, stamp, stamp) for a in apps],
            )
            self._db.execute(
                """UPDATE runs SET catalog_cursor = ?, catalog_pages = catalog_pages + 1, catalog_complete = ?
                   WHERE run_id = ?""",
                (cursor, int(complete), run_id),
            )

    def save_seeds(self, run_id: int, appids: Iterable[int], now: dt.datetime) -> None:
        """Register explicitly requested AppIDs (seed mode) and mark the catalog phase done.

        Existing rows keep their catalog name and last_modified.
        """
        stamp = iso(now)
        with self._db:
            self._db.executemany(
                """INSERT INTO discovered (appid, name, last_modified, price_change_number,
                                           first_seen_run, last_seen_run, first_seen_at, last_seen_at)
                   VALUES (?, '', NULL, NULL, ?, ?, ?, ?)
                   ON CONFLICT(appid) DO UPDATE SET last_seen_run = excluded.last_seen_run,
                                                    last_seen_at = excluded.last_seen_at""",
                [(a, run_id, run_id, stamp, stamp) for a in appids],
            )
            self._db.execute("UPDATE runs SET catalog_complete = 1 WHERE run_id = ?", (run_id,))

    def iter_discovered(self) -> Iterator[DiscoveredApp]:
        for row in self._db.execute("SELECT * FROM discovered ORDER BY appid"):
            yield DiscoveredApp(row["appid"], row["name"], row["last_modified"], row["price_change_number"],
                                row["first_seen_at"], row["last_seen_at"])

    def count_first_seen(self, run_id: int) -> int:
        return self._db.execute("SELECT COUNT(*) FROM discovered WHERE first_seen_run = ?", (run_id,)).fetchone()[0]

    # -- metadata ---------------------------------------------------------------------

    def pending(self, run: Run, legacy_ids: frozenset[int]) -> list[tuple[int, int | None]]:
        rows = self._db.execute(_PENDING_SQL, {"run": run.run_id, "recheck": int(run.recheck_unreleased)})
        return [(r["appid"], r["last_modified"]) for r in rows if r["appid"] not in legacy_ids]

    def record_facts(self, facts: AppFacts, *, source_last_modified: int | None, run_id: int, now: dt.datetime) -> None:
        with self._db:
            self._db.execute(
                """INSERT INTO facts (appid, facts_json, source_last_modified, recheckable, fetched_at, fetched_run)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(appid) DO UPDATE SET
                       facts_json = excluded.facts_json,
                       source_last_modified = excluded.source_last_modified,
                       recheckable = excluded.recheckable,
                       fetched_at = excluded.fetched_at,
                       fetched_run = excluded.fetched_run""",
                (facts.appid, facts.to_json(), source_last_modified, int(facts.needs_recheck), iso(now), run_id),
            )
            self._db.execute("DELETE FROM failures WHERE appid = ?", (facts.appid,))

    def record_failure(self, appid: int, *, error: str, detail: str, run_id: int, now: dt.datetime) -> None:
        with self._db:
            self._db.execute(
                """INSERT INTO failures (appid, error, detail, attempts, last_attempt_at, last_attempt_run)
                   VALUES (?, ?, ?, 1, ?, ?)
                   ON CONFLICT(appid) DO UPDATE SET
                       error = excluded.error,
                       detail = excluded.detail,
                       attempts = failures.attempts + 1,
                       last_attempt_at = excluded.last_attempt_at,
                       last_attempt_run = excluded.last_attempt_run""",
                (appid, error, detail, iso(now), run_id),
            )

    def all_facts(self) -> dict[int, StoredFacts]:
        return {
            r["appid"]: StoredFacts(AppFacts.from_json(r["facts_json"]), r["fetched_at"])
            for r in self._db.execute("SELECT appid, facts_json, fetched_at FROM facts")
        }

    def all_failures(self) -> dict[int, Failure]:
        return {
            r["appid"]: Failure(r["appid"], r["error"], r["detail"], r["attempts"], r["last_attempt_at"])
            for r in self._db.execute("SELECT * FROM failures")
        }

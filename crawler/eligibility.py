"""Decide whether a discovered app belongs in the new dataset.

``classify`` is a pure function of the app's store metadata (``AppFacts``),
the release-date window and the legacy AppIDs, so decisions can be recomputed
for a new window without any network access. Every decision carries one
reason code from ``REASONS`` plus a human-readable detail string.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from collections.abc import Container, Mapping
from dataclasses import dataclass
from typing import Any

from crawler.release_dates import ReleaseDate, parse_release_date

ELIGIBLE, EXCLUDED, FAILED = "eligible", "excluded", "failed"

# Reason codes -------------------------------------------------------------------
RELEASED_IN_WINDOW = "released_in_window"
IN_LEGACY_DATASET = "in_legacy_dataset"
STORE_DETAILS_UNAVAILABLE = "store_details_unavailable"
STORE_APPID_MISMATCH = "store_appid_mismatch"
APP_TYPE_MISSING = "app_type_missing"
DLC = "dlc"
DEMO = "demo"
SOUNDTRACK = "soundtrack"
SOFTWARE = "software"
NON_GAME_TYPE = "non_game_type"
LINKED_TO_PARENT_GAME = "linked_to_parent_game"
UNRELEASED = "unreleased"
RELEASE_DATE_MISSING = "release_date_missing"
RELEASE_DATE_AMBIGUOUS = "release_date_ambiguous"
RELEASED_BEFORE_WINDOW = "released_before_window"
RELEASED_AFTER_WINDOW = "released_after_window"
FETCH_ERROR = "fetch_error"

REASONS: Mapping[str, tuple[str, str]] = {
    RELEASED_IN_WINDOW: (ELIGIBLE, "released game whose whole release-date interval lies inside the window"),
    IN_LEGACY_DATASET: (EXCLUDED, "AppID already present in steamproducts2020.csv (never fetched)"),
    STORE_DETAILS_UNAVAILABLE: (EXCLUDED, "appdetails returned success=false (removed, unlisted or region-locked for cc=us)"),
    STORE_APPID_MISMATCH: (EXCLUDED, "appdetails answered with a different steam_appid (the request was redirected)"),
    APP_TYPE_MISSING: (EXCLUDED, "appdetails has no 'type', so the app cannot be confirmed to be a game"),
    DLC: (EXCLUDED, "type is 'dlc'"),
    DEMO: (EXCLUDED, "type is 'demo'"),
    SOUNDTRACK: (EXCLUDED, "type is 'music' (soundtrack)"),
    SOFTWARE: (EXCLUDED, "type is application/tool/config, or a 'game' whose genres are all software genres"),
    NON_GAME_TYPE: (EXCLUDED, "any other non-game type (video, series, episode, mod, hardware, advertising, ...)"),
    LINKED_TO_PARENT_GAME: (EXCLUDED, "type is 'game' but appdetails links a 'fullgame' parent (demo/playtest-style app)"),
    UNRELEASED: (EXCLUDED, "release_date.coming_soon is true"),
    RELEASE_DATE_MISSING: (EXCLUDED, "released, but the store gives no release date"),
    RELEASE_DATE_AMBIGUOUS: (EXCLUDED, "release date unparseable, or its interval straddles a window boundary"),
    RELEASED_BEFORE_WINDOW: (EXCLUDED, "release-date interval ends before the window start"),
    RELEASED_AFTER_WINDOW: (EXCLUDED, "release-date interval starts after the window end"),
    FETCH_ERROR: (FAILED, "appdetails request failed; retried on the next run"),
}

SOFTWARE_TYPES = frozenset({"application", "tool", "config"})
TYPE_REASONS = {"dlc": DLC, "demo": DEMO, "music": SOUNDTRACK} | {t: SOFTWARE for t in SOFTWARE_TYPES}

# Steam's non-game genre descriptions (English). "Early Access" and "Free to
# Play" describe a business model, so they are ignored when testing for these.
SOFTWARE_GENRES = frozenset({
    "Accounting", "Animation & Modeling", "Audio Production", "Design & Illustration", "Education",
    "Game Development", "Photo Editing", "Software Training", "Utilities", "Video Production", "Web Publishing",
})
NEUTRAL_GENRES = frozenset({"Early Access", "Free to Play"})


@dataclass(frozen=True)
class DateWindow:
    start: dt.date
    end: dt.date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"window start {self.start} is after end {self.end}")

    def __str__(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


@dataclass(frozen=True)
class AppFacts:
    """The subset of appdetails used for eligibility, extracted defensively."""

    appid: int
    success: bool
    steam_appid: int | None = None
    name: str | None = None
    type: str | None = None
    coming_soon: bool | None = None
    release_date_raw: str | None = None
    genres: tuple[str, ...] = ()
    fullgame_appid: int | None = None

    @classmethod
    def from_appdetails(cls, appid: int, data: Mapping[str, Any] | None) -> AppFacts:
        if data is None:
            return cls(appid, success=False)
        release = data.get("release_date")
        release = release if isinstance(release, dict) else {}
        coming_soon = release.get("coming_soon")
        raw_date = release.get("date")
        genres = data.get("genres")
        fullgame = data.get("fullgame")
        name, app_type = data.get("name"), data.get("type")
        return cls(
            appid=appid,
            success=True,
            steam_appid=_as_int(data.get("steam_appid")),
            name=name if isinstance(name, str) else None,
            type=app_type.strip().lower() if isinstance(app_type, str) and app_type.strip() else None,
            coming_soon=coming_soon if isinstance(coming_soon, bool) else None,
            release_date_raw=raw_date.strip() if isinstance(raw_date, str) else None,
            genres=tuple(
                g["description"] for g in genres if isinstance(g, dict) and isinstance(g.get("description"), str)
            ) if isinstance(genres, list) else (),
            fullgame_appid=_as_int(fullgame.get("appid")) if isinstance(fullgame, dict) else None,
        )

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> AppFacts:
        values = json.loads(text)
        values["genres"] = tuple(values.get("genres") or ())
        return cls(**values)

    @property
    def needs_recheck(self) -> bool:
        """True when a later fetch could change the decision (not yet released or no exact date)."""
        if not self.success:
            return False
        parsed = parse_release_date(self.release_date_raw)
        return self.coming_soon is True or parsed is None or parsed.exact is None


@dataclass(frozen=True)
class Decision:
    appid: int
    status: str
    reason: str
    detail: str
    release: ReleaseDate | None = None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _excluded(facts: AppFacts, reason: str, detail: str, release: ReleaseDate | None = None) -> Decision:
    return Decision(facts.appid, EXCLUDED, reason, detail, release)


def legacy_decision(appid: int) -> Decision:
    return Decision(appid, EXCLUDED, IN_LEGACY_DATASET, "appid present in steamproducts2020.csv")


def failed_decision(appid: int, error: str, detail: str) -> Decision:
    return Decision(appid, FAILED, FETCH_ERROR, f"{error}: {detail}")


def classify(facts: AppFacts, window: DateWindow, legacy_ids: Container[int]) -> Decision:
    """Apply the eligibility rules in a fixed order; the first matching rule wins."""
    if facts.appid in legacy_ids:
        return legacy_decision(facts.appid)
    if not facts.success:
        return _excluded(facts, STORE_DETAILS_UNAVAILABLE, "success=false")
    if facts.steam_appid is not None and facts.steam_appid != facts.appid:
        target = f"steam_appid={facts.steam_appid}"
        if facts.steam_appid in legacy_ids:
            target += " (in legacy dataset)"
        return _excluded(facts, STORE_APPID_MISMATCH, target)

    if facts.type is None:
        return _excluded(facts, APP_TYPE_MISSING, "type missing")
    if facts.type != "game":
        return _excluded(facts, TYPE_REASONS.get(facts.type, NON_GAME_TYPE), f"type={facts.type!r}")
    if facts.fullgame_appid is not None:
        return _excluded(facts, LINKED_TO_PARENT_GAME, f"fullgame_appid={facts.fullgame_appid}")
    substantive = [g for g in facts.genres if g not in NEUTRAL_GENRES]
    if substantive and all(g in SOFTWARE_GENRES for g in substantive):
        return _excluded(facts, SOFTWARE, f"genres={list(facts.genres)!r}")

    raw = facts.release_date_raw
    if facts.coming_soon is True:
        return _excluded(facts, UNRELEASED, f"coming_soon=true release_date={raw!r}")
    if not raw:
        return _excluded(facts, RELEASE_DATE_MISSING, f"coming_soon={_json_bool(facts.coming_soon)} release_date=''")

    release = parse_release_date(raw)
    if release is None:
        return _excluded(facts, RELEASE_DATE_AMBIGUOUS, f"unparseable release_date={raw!r}")
    interval = f"release_date={raw!r} precision={release.precision} interval={release.earliest}..{release.latest} window={window}"
    if release.latest < window.start:
        return _excluded(facts, RELEASED_BEFORE_WINDOW, interval, release)
    if release.earliest > window.end:
        return _excluded(facts, RELEASED_AFTER_WINDOW, interval, release)
    if release.earliest < window.start or release.latest > window.end:
        return _excluded(facts, RELEASE_DATE_AMBIGUOUS, f"straddles window boundary: {interval}", release)
    return Decision(facts.appid, ELIGIBLE, RELEASED_IN_WINDOW, interval, release)


def _json_bool(value: bool | None) -> str:
    return "null" if value is None else str(value).lower()

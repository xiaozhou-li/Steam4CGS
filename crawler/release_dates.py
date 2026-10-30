"""Parse Steam store release-date strings without inventing precision.

A parsed date is an inclusive interval ``[earliest, latest]`` with an explicit
precision. ``'2021'`` becomes 2021-01-01..2021-12-31 at ``year`` precision; it
is never turned into a specific day. Anything that is not unambiguously one
of the supported shapes (``'Early 2021'``, ``'03/04/2021'``, ``'TBA'``) parses
to ``None`` so the caller has to treat it as unknown.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass

DAY, MONTH, QUARTER, YEAR = "day", "month", "quarter", "year"

# English month names, hard-coded because calendar.month_name follows the locale.
_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
MONTHS: dict[str, int] = {name: i for i, name in enumerate(_MONTH_NAMES, 1)}
MONTHS.update({name[:3]: i for i, name in enumerate(_MONTH_NAMES, 1)})
MONTHS["sept"] = 9

YEAR_RANGE = (1970, 2100)

_M = r"(?P<month>[a-z]{3,9})\.?"
_Y = r"(?P<year>\d{4})"
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (precision, re.compile(pattern))
    for precision, pattern in (
        (DAY, rf"(?P<day>\d{{1,2}}) {_M},? {_Y}"),          # 3 Dec, 2019 | 3 December 2019
        (DAY, rf"{_M} (?P<day>\d{{1,2}}),? {_Y}"),          # Dec 3, 2019
        (DAY, rf"{_Y}-(?P<mnum>\d{{2}})-(?P<day>\d{{2}})"),  # 2019-12-03
        (MONTH, rf"{_M},? {_Y}"),                            # Dec 2020 | December, 2020
        (QUARTER, rf"q(?P<quarter>[1-4]),? {_Y}"),           # Q1 2021
        (QUARTER, rf"{_Y},? q(?P<quarter>[1-4])"),           # 2021 Q1
        (YEAR, _Y),                                          # 2021
    )
)


@dataclass(frozen=True)
class ReleaseDate:
    raw: str
    precision: str
    earliest: dt.date
    latest: dt.date

    @property
    def exact(self) -> dt.date | None:
        """The release day, only when the store gave a full date."""
        return self.earliest if self.precision == DAY else None


def parse_release_date(raw: str | None) -> ReleaseDate | None:
    if not raw:
        return None
    text = " ".join(raw.strip().lower().split())
    for precision, pattern in _PATTERNS:
        m = pattern.fullmatch(text)
        if m:
            return _build(raw, precision, m)
    return None


def _build(raw: str, precision: str, m: re.Match[str]) -> ReleaseDate | None:
    year = int(m["year"])
    if not YEAR_RANGE[0] <= year <= YEAR_RANGE[1]:
        return None
    groups = m.groupdict()
    if groups.get("mnum"):
        month = int(groups["mnum"])
    elif groups.get("month"):
        month = MONTHS.get(groups["month"])
        if month is None:
            return None
    else:
        month = None
    try:
        if precision == DAY:
            day = dt.date(year, month, int(groups["day"]))
            return ReleaseDate(raw, DAY, day, day)
        if precision == MONTH:
            last = calendar.monthrange(year, month)[1]
            return ReleaseDate(raw, MONTH, dt.date(year, month, 1), dt.date(year, month, last))
        if precision == QUARTER:
            first_month = 3 * (int(groups["quarter"]) - 1) + 1
            last_day = calendar.monthrange(year, first_month + 2)[1]
            return ReleaseDate(raw, QUARTER, dt.date(year, first_month, 1), dt.date(year, first_month + 2, last_day))
        return ReleaseDate(raw, YEAR, dt.date(year, 1, 1), dt.date(year, 12, 31))
    except ValueError:  # e.g. 31 Feb
        return None

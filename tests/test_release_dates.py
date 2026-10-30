import datetime as dt

import pytest

from crawler.release_dates import DAY, MONTH, QUARTER, YEAR, parse_release_date

D = dt.date


@pytest.mark.parametrize(
    "raw",
    ["3 Dec, 2019", "Dec 3, 2019", "December 3, 2019", "3 December 2019", "3 Dec 2019", "dec 3 2019",
     "2019-12-03", "  3   Dec,  2019 ", "3 Dec., 2019"],
)
def test_exact_dates(raw):
    r = parse_release_date(raw)
    assert (r.precision, r.earliest, r.latest, r.exact) == (DAY, D(2019, 12, 3), D(2019, 12, 3), D(2019, 12, 3))
    assert r.raw == raw  # the original text is kept verbatim


@pytest.mark.parametrize(
    ("raw", "precision", "earliest", "latest"),
    [
        ("Dec 2020", MONTH, D(2020, 12, 1), D(2020, 12, 31)),
        ("December, 2020", MONTH, D(2020, 12, 1), D(2020, 12, 31)),
        ("Feb 2024", MONTH, D(2024, 2, 1), D(2024, 2, 29)),
        ("Sept 2021", MONTH, D(2021, 9, 1), D(2021, 9, 30)),
        ("Q1 2021", QUARTER, D(2021, 1, 1), D(2021, 3, 31)),
        ("2021 Q4", QUARTER, D(2021, 10, 1), D(2021, 12, 31)),
        ("2021", YEAR, D(2021, 1, 1), D(2021, 12, 31)),
    ],
)
def test_imprecise_dates_become_intervals_not_days(raw, precision, earliest, latest):
    r = parse_release_date(raw)
    assert (r.precision, r.earliest, r.latest) == (precision, earliest, latest)
    assert r.exact is None  # no day is ever invented
    assert r.earliest < r.latest


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "TBA", "To be announced", "Coming soon", "Early 2021", "Summer 2021", "Late 2020",
     "03/04/2021",        # DD/MM vs MM/DD is genuinely ambiguous
     "31 Feb, 2021", "Feb 30, 2021", "Q5 2021", "Smarch 3, 2021", "2021年3月3日", "1899", "3 Dec, 19",
     "When it's done", "2020-13-01", "Dec 2020 - Jan 2021"],
)
def test_ambiguous_or_invalid_dates_are_not_parsed(raw):
    assert parse_release_date(raw) is None

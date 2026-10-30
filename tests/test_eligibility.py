import datetime as dt

import pytest

from crawler import eligibility as e
from crawler.eligibility import AppFacts, DateWindow, classify

WINDOW = DateWindow(dt.date(2020, 1, 1), dt.date(2026, 10, 30))


def facts(appid=100, **kw):
    base = dict(success=True, steam_appid=appid, name="G", type="game", coming_soon=False,
                release_date_raw="3 Mar, 2021", genres=("Action",))
    base.update(kw)
    return AppFacts(appid, **base)


def reason(f, legacy=frozenset(), window=WINDOW):
    d = classify(f, window, legacy)
    return d.status, d.reason


def test_every_reason_is_documented():
    statuses = {e.ELIGIBLE, e.EXCLUDED, e.FAILED}
    codes = {v for k, v in vars(e).items() if k.isupper() and isinstance(v, str)} - statuses
    assert codes == set(e.REASONS)
    assert {status for status, _ in e.REASONS.values()} == {e.ELIGIBLE, e.EXCLUDED, e.FAILED}


def test_eligible():
    d = classify(facts(), WINDOW, frozenset())
    assert (d.status, d.reason) == (e.ELIGIBLE, e.RELEASED_IN_WINDOW)
    assert "precision=day" in d.detail and "window=2020-01-01..2026-10-30" in d.detail


def test_legacy_wins_over_everything():
    assert reason(facts(), legacy={100}) == (e.EXCLUDED, e.IN_LEGACY_DATASET)


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"success": False}, e.STORE_DETAILS_UNAVAILABLE),
        ({"steam_appid": 200}, e.STORE_APPID_MISMATCH),
        ({"type": None}, e.APP_TYPE_MISSING),
        ({"type": "dlc"}, e.DLC),
        ({"type": "demo"}, e.DEMO),
        ({"type": "music"}, e.SOUNDTRACK),
        ({"type": "application"}, e.SOFTWARE),
        ({"type": "tool"}, e.SOFTWARE),
        ({"type": "video"}, e.NON_GAME_TYPE),
        ({"type": "mod"}, e.NON_GAME_TYPE),
        ({"fullgame_appid": 50}, e.LINKED_TO_PARENT_GAME),
        ({"genres": ("Utilities", "Design & Illustration")}, e.SOFTWARE),
        ({"genres": ("Utilities", "Early Access")}, e.SOFTWARE),
        ({"coming_soon": True, "release_date_raw": "3 Mar, 2021"}, e.UNRELEASED),
        ({"coming_soon": True, "release_date_raw": "Coming soon"}, e.UNRELEASED),
        ({"release_date_raw": ""}, e.RELEASE_DATE_MISSING),
        ({"release_date_raw": None, "coming_soon": None}, e.RELEASE_DATE_MISSING),
        ({"release_date_raw": "Early 2021"}, e.RELEASE_DATE_AMBIGUOUS),
        ({"release_date_raw": "03/04/2021"}, e.RELEASE_DATE_AMBIGUOUS),
        ({"release_date_raw": "31 Dec, 2019"}, e.RELEASED_BEFORE_WINDOW),
        ({"release_date_raw": "2019"}, e.RELEASED_BEFORE_WINDOW),
        ({"release_date_raw": "1 Nov, 2026"}, e.RELEASED_AFTER_WINDOW),
    ],
)
def test_exclusion_reasons(kw, expected):
    assert reason(facts(**kw)) == (e.EXCLUDED, expected)


def test_game_with_mixed_genres_is_not_software():
    assert reason(facts(genres=("Education", "Casual"))) == (e.ELIGIBLE, e.RELEASED_IN_WINDOW)


def test_mismatch_detail_flags_legacy_target():
    d = classify(facts(steam_appid=7), WINDOW, frozenset({7}))
    assert d.reason == e.STORE_APPID_MISMATCH and "(in legacy dataset)" in d.detail


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 Jan, 2020", e.RELEASED_IN_WINDOW),    # inclusive start
        ("30 Oct, 2026", e.RELEASED_IN_WINDOW),   # inclusive end
        ("2020", e.RELEASED_IN_WINDOW),           # whole year inside the window
        ("Q4 2020", e.RELEASED_IN_WINDOW),
        ("Oct 2026", e.RELEASE_DATE_AMBIGUOUS),   # Oct 1..31 straddles the end
        ("2026", e.RELEASE_DATE_AMBIGUOUS),
        ("Dec 2019", e.RELEASED_BEFORE_WINDOW),
    ],
)
def test_window_boundaries_use_the_whole_interval(raw, expected):
    assert classify(facts(release_date_raw=raw), WINDOW, frozenset()).reason == expected


def test_straddling_interval_with_custom_start():
    window = DateWindow(dt.date(2020, 6, 15), dt.date(2021, 1, 1))
    assert classify(facts(release_date_raw="Jun 2020"), window, frozenset()).reason == e.RELEASE_DATE_AMBIGUOUS


def test_window_validation():
    with pytest.raises(ValueError):
        DateWindow(dt.date(2021, 1, 2), dt.date(2021, 1, 1))


def test_from_appdetails_is_defensive():
    f = AppFacts.from_appdetails(5, {
        "type": " Game ", "name": "X", "steam_appid": 5,
        "release_date": {"coming_soon": "no", "date": " 3 Mar, 2021 "},
        "genres": [{"id": "1", "description": "Action"}, "junk", {"id": "2"}],
        "fullgame": {"appid": "abc"},
    })
    assert f.type == "game" and f.coming_soon is None and f.release_date_raw == "3 Mar, 2021"
    assert f.genres == ("Action",) and f.fullgame_appid is None
    assert AppFacts.from_appdetails(5, None) == AppFacts(5, success=False)
    assert AppFacts.from_appdetails(5, {}).type is None


def test_facts_json_roundtrip():
    f = facts(genres=("Action", "Indie"), fullgame_appid=9)
    assert AppFacts.from_json(f.to_json()) == f


@pytest.mark.parametrize(
    ("kw", "recheck"),
    [({}, False), ({"coming_soon": True}, True), ({"release_date_raw": "2021"}, True),
     ({"release_date_raw": "TBA"}, True), ({"success": False}, False)],
)
def test_needs_recheck(kw, recheck):
    assert facts(**kw).needs_recheck is recheck

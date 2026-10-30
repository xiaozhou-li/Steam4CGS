"""Field-mapping tests: Steam source payloads -> legacy CSV cells + provenance."""

from __future__ import annotations

import collections
import copy
import csv
import graphlib
import ast

import pytest

from crawler import metadata as m
from crawler import schema
from crawler.metadata import ExtractionError, SourceResult, normalize

AT = "2026-10-30T12:00:00Z"
TAGS = {492: "Indie", 19: "Action", 7208: "Point & Click", 493: "Early Access"}


def details(**overrides):
    data = {
        "type": "game", "name": "Kestrel", "steam_appid": 10, "is_free": False,
        "developers": ["Dev A"], "publishers": ["Pub A"],
        "genres": [{"id": "1", "description": "Action"}],
        "categories": [{"id": 2, "description": "Single-player"}],
        "price_overview": {"currency": "USD", "initial": 1999, "final": 1999, "discount_percent": 0},
        "release_date": {"coming_soon": False, "date": "3 Dec, 2021"},
        "metacritic": {"score": 77, "url": "https://example.invalid/m"},
    }
    for k, v in overrides.items():
        if v is ...:
            data.pop(k, None)
        else:
            data[k] = v
    return data


def src(name, data=None, error=None):
    return SourceResult(name, f"https://store.steampowered.com/{name}", AT, data=data, error=error)


def run(data=None, *, reviews=None, tags=(492, 19), tag_names=TAGS, reviews_error=None, tags_error=None):
    return normalize(
        10,
        src("appdetails", details() if data is None else data),
        src("appreviews", {"review_score_desc": "Very Positive", "total_reviews": 500} if reviews is None else reviews,
            reviews_error),
        src("store_items", list(tags) if tags is not None else None, tags_error),
        tag_names,
    )


def cell(result, column):
    return result.row[column]


def prov(result, column):
    return next(p for p in result.provenance if p.field == column)


# --- whole row --------------------------------------------------------------------------

def test_row_has_exact_columns_and_validates():
    r = run()
    assert tuple(r.row) == schema.COLUMNS
    assert schema.validate_row(r.row) == []
    assert [p.field for p in r.provenance] == list(schema.COLUMNS)
    assert r.complete


def test_full_mapping():
    assert run().row == {
        "id": "10", "title": "Kestrel", "genres": "['Action']", "developer": "Dev A", "publisher": "Pub A",
        "release_date": "3 Dec, 2021", "app_name": "Kestrel", "specs": "['Single-player']",
        "tags": "['Indie', 'Action']", "price": "20", "sentiment": "Very Positive", "early_access": "FALSE",
        "metascore": "77",
    }


@pytest.mark.parametrize(
    ("data", "message"),
    [(None, "success=false"), (details(steam_appid=11), "steam_appid=11"), (details(name=" "), "no name"),
     (details(release_date={"coming_soon": False, "date": ""}), "release_date")],
)
def test_unbuildable_rows_raise(data, message):
    appdetails = src("appdetails", data) if data is not None else src("appdetails", None)
    with pytest.raises(ExtractionError, match=message):
        normalize(10, appdetails, src("appreviews", {}), src("store_items", []), TAGS)


def test_appdetails_request_failure_raises():
    with pytest.raises(ExtractionError, match="unavailable"):
        normalize(10, src("appdetails", error="SteamHTTPError: 500"), src("appreviews", {}), src("store_items", []), TAGS)


# --- title / app_name ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "app_name", "title"),
    [("Sword & Shield", "Sword & Shield", "Sword &amp; Shield"),
     ("Sword &amp; Shield", "Sword & Shield", "Sword &amp; Shield"),  # pre-escaped input is normalized
     ("A <B>", "A <B>", "A &lt;B&gt;"),
     ("It's \"quoted\"", "It's \"quoted\"", "It's \"quoted\""),
     ("  Padded  ", "Padded", "Padded")],
)
def test_title_and_app_name(name, app_name, title):
    r = run(details(name=name))
    assert (cell(r, "app_name"), cell(r, "title")) == (app_name, title)


# --- genres / early_access ------------------------------------------------------------------

def _genres(*names):
    return [{"id": str(i), "description": n} for i, n in enumerate(names)]


def test_genres_follow_legacy_order():
    r = run(details(genres=_genres("RPG", "Early Access", "Racing", "Indie", "Action")))
    assert cell(r, "genres") == "['Action', 'Indie', 'Racing', 'RPG', 'Early Access']"
    assert cell(r, "early_access") == "TRUE"


def test_genres_are_html_escaped_like_legacy():
    r = run(details(genres=_genres("Animation & Modeling", "Design &amp; Illustration")))
    assert cell(r, "genres") == "['Animation &amp; Modeling', 'Design &amp; Illustration']"


@pytest.mark.parametrize("genres", [..., [], None, "Action", [{"id": "1"}]])
def test_missing_genres_leave_early_access_unverified(genres):
    r = run(details(genres=genres))
    assert cell(r, "genres") == "" and prov(r, "genres").status == m.MISSING
    assert cell(r, "early_access") == "" and prov(r, "early_access").status == m.UNVERIFIED


def test_early_access_never_comes_from_tags():
    r = run(details(genres=...), tags=[493])
    assert cell(r, "tags") == "['Early Access']"
    assert cell(r, "early_access") == ""


def test_genres_derived_order_matches_legacy_data(legacy_csv):
    with open(legacy_csv, newline="", encoding="utf-8") as f:
        lists = [ast.literal_eval(r["genres"]) for r in csv.DictReader(f) if r["genres"]]
    matches = 0
    for genres in lists:
        r = run(details(genres=_genres(*genres[::-1])))
        matches += cell(r, "genres") == repr(genres)
    assert matches / len(lists) > 0.997  # 22,929 of 22,997 legacy lists


# --- developer / publisher ------------------------------------------------------------------

def test_developers_and_publishers_are_joined_and_escaped():
    r = run(details(developers=["Studio A", "Studio B & Sons", "Studio A"], publishers=["X", " "]))
    assert cell(r, "developer") == "Studio A, Studio B &amp; Sons"
    assert cell(r, "publisher") == "X"
    assert "Franchise:" not in cell(r, "publisher")


@pytest.mark.parametrize("value", [..., [], [""], None, "Dev"])
def test_missing_developer_is_blank(value):
    r = run(details(developers=value))
    assert cell(r, "developer") == "" and prov(r, "developer").status == m.MISSING


# --- release_date ---------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected", "note"),
    [("Dec 3, 2021", "3 Dec, 2021", "exact"), ("3 December, 2021", "3 Dec, 2021", "exact"),
     ("2021-12-03", "3 Dec, 2021", "exact"), ("03 Dec, 2021", "3 Dec, 2021", "exact"),
     ("Q3 2022", "Q3 2022", "precision=quarter"), ("Dec 2021", "Dec 2021", "precision=month"),
     ("2022", "2022", "precision=year"), ("Coming soon", "Coming soon", "precision=unparsed")],
)
def test_release_date_format(raw, expected, note):
    r = run(details(release_date={"coming_soon": False, "date": raw}))
    assert cell(r, "release_date") == expected
    assert note in prov(r, "release_date").note
    assert prov(r, "release_date").raw == f'{{"coming_soon": false, "date": "{raw}"}}'


def test_unparseable_date_like_text_is_rejected_not_rewritten():
    with pytest.raises(ExtractionError, match="schema"):
        run(details(release_date={"coming_soon": False, "date": "31 Feb, 2021"}))


# --- specs ------------------------------------------------------------------------------------------

def _cats(*labels):
    return [{"id": i, "description": label} for i, label in enumerate(labels)]


def test_specs_use_store_categories_in_legacy_order():
    r = run(details(categories=_cats("Steam Cloud", "Multi-player", "Steam Achievements", "Single-player",
                                     "Family Sharing", "Full controller support")))
    assert cell(r, "specs") == ("['Single-player', 'Multi-player', 'Steam Achievements', "
                                "'Full controller support', 'Steam Cloud', 'Family Sharing']")
    assert "Family Sharing" in prov(r, "specs").note


def test_steam_achievements_only_when_category_present():
    with_cat = run(details(categories=_cats("Single-player", "Steam Achievements")))
    without = run(details(categories=_cats("Single-player"), achievements={"total": 12}))
    assert "Steam Achievements" in cell(with_cat, "specs")
    assert "Steam Achievements" not in cell(without, "specs")


def test_single_player_is_not_invented():
    r = run(details(categories=_cats("Multi-player", "Online PvP")))
    assert cell(r, "specs") == "['Multi-player', 'Online PvP']"


@pytest.mark.parametrize("value", [..., [], None])
def test_missing_categories_leave_specs_blank(value):
    r = run(details(categories=value))
    assert cell(r, "specs") == "" and prov(r, "specs").status == m.MISSING


def test_spec_order_matches_every_legacy_row(legacy_csv):
    with open(legacy_csv, newline="", encoding="utf-8") as f:
        rows = [[s for s in ast.literal_eval(r["specs"]) if s] for r in csv.DictReader(f)]
    ts = graphlib.TopologicalSorter()
    for lst in rows:
        for a, b in zip(lst, lst[1:]):
            ts.add(b, a)
    derived = [s for s in ts.static_order() if not s.endswith("\xa0")]
    rank = {s: i for i, s in enumerate(m.SPEC_ORDER)}
    assert set(derived) == set(m.SPEC_ORDER)
    assert all([rank[s] for s in lst if s in rank] == sorted(rank[s] for s in lst if s in rank) for lst in rows)


# --- tags -----------------------------------------------------------------------------------------------

def test_tags_keep_steam_order_and_raw_ampersand():
    r = run(tags=[7208, 492, 19])
    assert cell(r, "tags") == "['Point & Click', 'Indie', 'Action']"


def test_tags_are_capped_at_twenty():
    names = {i: f"T{i}" for i in range(1, 30)}
    r = run(tags=list(range(1, 30)), tag_names=names)
    assert len(schema.decode_list(cell(r, "tags"))) == 20


def test_unknown_tag_ids_are_skipped_not_invented():
    r = run(tags=[492, 555, 19])
    assert cell(r, "tags") == "['Indie', 'Action']" and "555" in prov(r, "tags").note


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [({"tags": []}, m.MISSING), ({"tags": None}, m.MISSING),
     ({"tags_error": "SteamHTTPError: 500"}, m.SOURCE_UNAVAILABLE), ({"tag_names": None}, m.SOURCE_UNAVAILABLE),
     ({"tags": [555]}, m.MISSING)],
)
def test_missing_tags_are_never_replaced_with_genres(kwargs, status):
    r = run(details(genres=_genres("Action", "Indie")), **kwargs)
    assert cell(r, "tags") == ""
    assert prov(r, "tags").status == status
    assert cell(r, "genres") == "['Action', 'Indie']"
    assert r.complete is (status != m.SOURCE_UNAVAILABLE)


# --- price ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("price_overview", "is_free", "expected", "status"),
    [
        ({"currency": "USD", "initial": 1999, "final": 1999, "discount_percent": 0}, False, "20", m.PRESENT),
        ({"currency": "USD", "initial": 1999, "final": 499, "discount_percent": 75}, False, "20", m.PRESENT),
        ({"currency": "USD", "initial": 99, "final": 99, "discount_percent": 0}, False, "1", m.PRESENT),
        ({"currency": "USD", "initial": 449, "final": 449, "discount_percent": 0}, False, "4", m.PRESENT),
        ({"currency": "USD", "initial": 450, "final": 450, "discount_percent": 0}, False, "5", m.PRESENT),
        ({"currency": "USD", "final": 1499, "discount_percent": 0}, False, "15", m.PRESENT),
        ({"currency": "USD", "final": 1499, "discount_percent": 25}, False, "", m.MISSING),
        ({"currency": "EUR", "initial": 1999, "final": 1999, "discount_percent": 0}, False, "", m.UNRECOGNIZED),
        (..., True, "0", m.PRESENT),
        (..., False, "", m.MISSING),
        (..., ..., "", m.MISSING),
    ],
)
def test_price_mapping(price_overview, is_free, expected, status):
    r = run(details(price_overview=price_overview, is_free=is_free))
    assert cell(r, "price") == expected and prov(r, "price").status == status


def test_sale_price_is_recorded_but_not_used():
    po = {"currency": "USD", "initial": 5999, "final": 1499, "discount_percent": 75}
    p = prov(run(details(price_overview=po)), "price")
    assert p.value == "60" and p.source == "appdetails:price_overview.initial"
    assert '"final": 1499' in p.raw and '"initial": 5999' in p.raw and '"currency": "USD"' in p.raw
    assert "sale price ignored" in p.note and m.PRICE_CONVERSION in p.note


# --- sentiment --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("label", list(schema.SENTIMENT_LABELS) + ["1 user reviews", "9 user reviews"])
def test_sentiment_uses_store_label_verbatim(label):
    r = run(reviews={"review_score_desc": label, "total_reviews": 1})
    assert cell(r, "sentiment") == label and prov(r, "sentiment").status == m.PRESENT


@pytest.mark.parametrize(
    ("reviews", "error", "status"),
    [({"review_score_desc": "No user reviews", "total_reviews": 0}, None, m.MISSING),
     ({"total_reviews": 0}, None, m.MISSING),
     ({"review_score_desc": "Très positives", "total_reviews": 50}, None, m.UNRECOGNIZED),
     ({"review_score": 8, "total_positive": 90, "total_negative": 10, "total_reviews": 100}, None, m.UNRECOGNIZED),
     ({}, "SteamHTTPError: 502", m.SOURCE_UNAVAILABLE)],
)
def test_sentiment_is_never_fabricated(reviews, error, status):
    r = run(reviews=reviews, reviews_error=error)
    assert cell(r, "sentiment") == "" and prov(r, "sentiment").status == status


# --- metascore ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("metacritic", "expected", "status"),
    [({"score": 77}, "77", m.PRESENT), ({"score": 0}, "0", m.PRESENT), (..., "", m.MISSING),
     ({"score": "77"}, "", m.UNRECOGNIZED), ({"score": 101}, "", m.UNRECOGNIZED), ({"score": True}, "", m.UNRECOGNIZED)],
)
def test_metascore_mapping(metacritic, expected, status):
    r = run(details(metacritic=metacritic))
    assert cell(r, "metascore") == expected and prov(r, "metascore").status == status


# --- provenance -------------------------------------------------------------------------------------------------------

def test_provenance_records_source_time_and_raw_values():
    r = run()
    for p in r.provenance:
        assert p.appid == 10 and p.retrieved_at == AT and p.source_url.startswith("https://")
        assert p.value == r.row[p.field]
    assert prov(r, "metascore").raw == '{"score": 77, "url": "https://example.invalid/m"}'
    assert prov(r, "sentiment").source == "appreviews:query_summary.review_score_desc"


def test_provenance_statuses_cover_missing_fields():
    sparse = details(genres=..., developers=..., publishers=..., categories=..., price_overview=..., metacritic=...)
    r = run(sparse, reviews={"review_score_desc": "No user reviews", "total_reviews": 0}, tags=[])
    blanks = {p.field: p.status for p in r.provenance if p.value == ""}
    assert blanks == {
        "genres": m.MISSING, "developer": m.MISSING, "publisher": m.MISSING, "specs": m.MISSING, "tags": m.MISSING,
        "price": m.MISSING, "sentiment": m.MISSING, "early_access": m.UNVERIFIED, "metascore": m.MISSING,
    }
    assert schema.validate_row(r.row) == []


def test_normalize_does_not_mutate_input():
    data = details(genres=_genres("RPG", "Action"))
    before = copy.deepcopy(data)
    run(data)
    assert data == before

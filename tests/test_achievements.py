"""Achievement variables: parsing, unavailable-vs-zero semantics, statistics, collection and isolation."""

from __future__ import annotations

import csv
import hashlib
import shutil

import pytest
from fakes import FAKE_KEY, make_transport
from sample_data import APPDETAILS, SampleSteam

from crawler import achievements as A
from crawler import cli, pipeline
from crawler.transport import RetryPolicy

AT = "2026-10-30T12:00:00Z"


def out(status, count=None, items=(), category=None):
    return A.Outcome(status, "https://x", AT, count=count, items=tuple(items), category=category)


def record(store=None, schema=None, pct=None):
    return A.Record(10, AT, store or out(A.NO_DATA), schema or out(A.NOT_ATTEMPTED), pct or out(A.NOT_AVAILABLE))


def pct_ok(*values):
    items = [(f"A{i}", v) for i, v in enumerate(values)]
    return out(A.OK, len(items), items)


def features(**kw):
    return A.derive(record(**kw))[0]


# --- parsing ------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (None, (A.NOT_AVAILABLE, None, None)),
        ({"categories": [{"description": "Single-player"}]}, (A.NO_DATA, None, False)),
        ({}, (A.NO_DATA, None, None)),
        ({"categories": [{"description": "Steam Achievements"}]}, (A.OK, None, True)),
        ({"achievements": {"total": 0}}, (A.EMPTY, 0, None)),
        ({"achievements": {"total": 12}, "categories": []}, (A.OK, 12, False)),
        ({"achievements": {"total": "12"}}, (A.UNRECOGNIZED, None, None)),
        ({"achievements": {"total": -1}}, (A.UNRECOGNIZED, None, None)),
    ],
)
def test_parse_store(data, expected):
    assert A.parse_store(data)[:3] == expected


@pytest.mark.parametrize(
    ("body", "status", "n"),
    [
        ({}, A.UNRECOGNIZED, 0),
        ({"game": {}}, A.NO_DATA, 0),
        ({"game": {"gameName": "G"}}, A.EMPTY, 0),
        ({"game": {"gameName": "G", "availableGameStats": {"stats": []}}}, A.EMPTY, 0),
        ({"game": {"gameName": "G", "availableGameStats": {"achievements": []}}}, A.EMPTY, 0),
        ({"game": {"gameName": "G", "availableGameStats": {"achievements": [{"name": "A", "hidden": 1}]}}}, A.OK, 1),
        ({"game": {"gameName": "G", "availableGameStats": {"achievements": [{"name": "A"}, {"name": "A"}]}}},
         A.UNRECOGNIZED, 0),
        ({"game": {"gameName": "G", "availableGameStats": {"achievements": "x"}}}, A.UNRECOGNIZED, 0),
        ({"game": {"gameName": "G", "availableGameStats": []}}, A.UNRECOGNIZED, 0),
    ],
)
def test_parse_schema(body, status, n):
    s, items, _ = A.parse_schema(body)
    assert (s, len(items)) == (status, n)


def test_parse_schema_keeps_display_names_and_hidden():
    _, items, _ = A.parse_schema({"game": {"gameName": "G", "availableGameStats": {"achievements": [
        {"name": "A", "displayName": "First", "hidden": 0}, {"name": "B", "hidden": 1}, {"name": "C", "hidden": 7}]}}})
    assert items == (("A", "First", False), ("B", "", True), ("C", "", None))


@pytest.mark.parametrize(
    ("achievements", "status", "values"),
    [
        ([{"name": "A", "percent": 71.2}, {"name": "B", "percent": "9.5"}], A.OK, (71.2, 9.5)),  # numbers or strings
        ([{"name": "A", "percent": 0}, {"name": "B", "percent": 100}], A.OK, (0.0, 100.0)),
        ([], A.EMPTY, ()),
        ([{"name": "A", "percent": -1}], A.UNRECOGNIZED, ()),
        ([{"name": "A", "percent": 100.5}], A.UNRECOGNIZED, ()),
        ([{"name": "A", "percent": "abc"}], A.UNRECOGNIZED, ()),
        ([{"name": "A", "percent": float("nan")}], A.UNRECOGNIZED, ()),
        ([{"name": "A", "percent": True}], A.UNRECOGNIZED, ()),
        ([{"name": "A", "percent": None}], A.UNRECOGNIZED, ()),
        ([{"name": "", "percent": 5}], A.UNRECOGNIZED, ()),
        ([{"name": "A", "percent": 5}, {"name": "A", "percent": 6}], A.UNRECOGNIZED, ()),
    ],
)
def test_parse_percentages(achievements, status, values):
    s, items, _ = A.parse_percentages({"achievementpercentages": {"achievements": achievements}})
    assert s == status and tuple(p for _, p in items) == values


@pytest.mark.parametrize("body", [{}, {"achievementpercentages": []}, {"achievementpercentages": {"achievements": "x"}}])
def test_parse_percentages_shape_errors(body):
    assert A.parse_percentages(body)[0] == A.UNRECOGNIZED


def test_parse_percentages_without_list_is_no_data():
    assert A.parse_percentages({"achievementpercentages": {}})[0] == A.NO_DATA


# --- unavailable vs zero ---------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("kw", "has", "count", "source", "available"),
    [
        # nothing known: blank everywhere, never zero
        ({}, "", "", "", "FALSE"),
        ({"pct": out(A.ERROR)}, "", "", "", ""),
        ({"store": out(A.NOT_AVAILABLE), "pct": out(A.ERROR)}, "", "", "", ""),
        ({"store": out(A.NO_DATA, category=False), "schema": out(A.NO_DATA), "pct": out(A.UNRECOGNIZED)}, "", "", "", ""),
        # explicit zeros
        ({"pct": out(A.EMPTY, 0)}, "FALSE", "0", "global_percentages", "FALSE"),
        ({"schema": out(A.EMPTY, 0)}, "FALSE", "0", "schema", "FALSE"),
        ({"store": out(A.EMPTY, 0)}, "FALSE", "0", "store_summary", "FALSE"),
        ({"schema": out(A.EMPTY, 0), "pct": out(A.EMPTY, 0)}, "FALSE", "0", "schema", "FALSE"),
        # positive evidence
        ({"store": out(A.OK, None, category=True)}, "TRUE", "", "", "FALSE"),
        ({"store": out(A.OK, 5)}, "TRUE", "5", "store_summary", "FALSE"),
        ({"store": out(A.OK, 5), "pct": pct_ok(10, 20, 30)}, "TRUE", "3", "global_percentages", "TRUE"),
        ({"store": out(A.OK, 5), "schema": out(A.OK, 4, [("a", "", False)] * 4), "pct": pct_ok(10, 20, 30)},
         "TRUE", "4", "schema", "TRUE"),
        # conflict: positive evidence wins for presence; the count comes from the positive source
        ({"store": out(A.OK, 20), "pct": out(A.EMPTY, 0)}, "TRUE", "20", "store_summary", "FALSE"),
    ],
)
def test_presence_count_and_availability(kw, has, count, source, available):
    f = features(**kw)
    assert (f["has_steam_achievements"], f["achievement_count"], f["achievement_count_source"],
            f["global_unlock_percentages_available"]) == (has, count, source, available)
    if available != "TRUE":
        assert all(f[c] == "" for c in ("mean_global_unlock_pct", "median_global_unlock_pct",
                                         "min_global_unlock_pct", "share_below_10pct", "share_below_1pct"))


def test_conflict_is_flagged_in_sources():
    _, _, src = A.derive(record(store=out(A.OK, 20), pct=out(A.EMPTY, 0)))
    assert src["conflict"] == "TRUE"
    assert A.derive(record())[2]["conflict"] == ""


def test_unlock_statistics():
    f = features(pct=pct_ok(71.2, 9.5, 0.8, 0.1))
    assert (f["mean_global_unlock_pct"], f["median_global_unlock_pct"], f["min_global_unlock_pct"],
            f["share_below_10pct"], f["share_below_1pct"]) == ("20.4", "5.15", "0.1", "0.75", "0.5")


def test_thresholds_are_strict():
    f = features(pct=pct_ok(10.0, 1.0, 9.9999, 0.9999))
    assert (f["share_below_10pct"], f["share_below_1pct"]) == ("0.75", "0.25")


def test_all_zero_percentages_are_real_data():
    f = features(pct=pct_ok(0, 0))
    assert (f["global_unlock_percentages_available"], f["mean_global_unlock_pct"], f["share_below_1pct"]) == ("TRUE", "0", "1")


@pytest.mark.parametrize(("x", "text"), [(0.1 + 0.2, "0.3"), (5.0, "5"), (1 / 3, "0.3333"), (0.0, "0"), (None, ""),
                                         (45.299999237060547, "45.3"), (-0.00001, "0")])
def test_fmt(x, text):
    assert A.fmt(x) == text


def test_per_achievement_union():
    schema = out(A.OK, 2, [("A", "Alpha", False), ("B", "Beta", True)])
    pct = out(A.OK, 2, [("A", 50.0), ("Z", 1.5)])
    _, rows, _ = A.derive(record(schema=schema, pct=pct))
    assert [(r["achievement_api_name"], r["display_name"], r["hidden"], r["in_schema"], r["global_unlock_pct"])
            for r in rows] == [("A", "Alpha", "FALSE", "TRUE", "50"), ("B", "Beta", "TRUE", "TRUE", ""),
                               ("Z", "", "", "FALSE", "1.5")]
    _, rows, _ = A.derive(record(pct=pct))
    assert [(r["achievement_api_name"], r["in_schema"]) for r in rows] == [("A", ""), ("Z", "")]


def test_record_json_roundtrip():
    r = record(schema=out(A.OK, 1, [("A", "x", None)]), pct=pct_ok(1.5))
    assert A.Record.from_json(r.to_json()) == r


# --- collection end to end ---------------------------------------------------------------------------------

CATALOG = {a: 1_700_000_000 for a in APPDETAILS}


@pytest.fixture(scope="module")
def crawled(tmp_path_factory, legacy_csv):
    base = tmp_path_factory.mktemp("ach")
    argv = ["run", "--base-dir", str(base), "--work-dir", str(base / "work"), "--legacy-csv", str(legacy_csv),
            "--end-date", "2026-10-30"]
    adapter = SampleSteam(catalog=CATALOG)
    assert cli.main(argv, environ={"STEAM_API_KEY": FAKE_KEY},
                    transport=make_transport(adapter, retry=RetryPolicy(max_attempts=1))) == 0
    return base


@pytest.fixture
def ws(crawled, tmp_path):
    copy = tmp_path / "ws"
    shutil.copytree(crawled, copy)
    return copy


def ach(base, legacy_csv, adapter, *extra, key=None):
    argv = ["achievements", "--base-dir", str(base), "--work-dir", str(base / "work"), "--legacy-csv",
            str(legacy_csv), *extra]
    return cli.main(argv, environ={"STEAM_API_KEY": key} if key else {},
                    transport=make_transport(adapter, retry=RetryPolicy(max_attempts=1)))


def read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def calls(adapter, fragment):
    return [q for p, q in adapter.calls if fragment in p]


def test_features_without_key(ws, legacy_csv):
    adapter = SampleSteam()
    assert ach(ws, legacy_csv, adapter, "--scope", "new") == 0
    with open(ws / A.FEATURES_CSV, newline="", encoding="utf-8") as f:
        assert tuple(next(csv.reader(f))) == A.FEATURE_FIELDS
    f = {int(r["appid"]): r for r in read(ws / A.FEATURES_CSV)}
    assert set(f) == {9900001, 9900002, 9900003, 9900004}
    # 9900001: percentages (4) beat the store summary (40); there is no schema without a key
    assert (f[9900001]["has_steam_achievements"], f[9900001]["achievement_count"],
            f[9900001]["achievement_count_source"], f[9900001]["global_unlock_percentages_available"]) == \
        ("TRUE", "4", "global_percentages", "TRUE")
    assert f[9900001]["mean_global_unlock_pct"] == "20.4" and f[9900001]["snapshot_utc"]
    # 9900002: no summary, no category, percentages HTTP 403 -> unknown, not zero
    assert (f[9900002]["has_steam_achievements"], f[9900002]["achievement_count"],
            f[9900002]["global_unlock_percentages_available"]) == ("", "", "FALSE")
    # 9900003: explicit empty percentage list -> zero
    assert (f[9900003]["has_steam_achievements"], f[9900003]["achievement_count"],
            f[9900003]["achievement_count_source"]) == ("FALSE", "0", "global_percentages")
    # 9900004: category says yes; the percentages request failed -> TRUE, count and availability unknown
    assert (f[9900004]["has_steam_achievements"], f[9900004]["achievement_count"],
            f[9900004]["global_unlock_percentages_available"]) == ("TRUE", "", "")
    assert calls(adapter, "GetSchemaForGame") == []
    assert calls(adapter, "/api/appdetails") == []  # Store summary reused from the appdetails cache
    sources = {int(s["appid"]): s for s in read(ws / "work" / "achievements" / "achievement_sources.csv")}
    assert sources[9900001]["schema_status"] == A.NOT_ATTEMPTED and sources[9900004]["pct_status"] == A.ERROR
    per = read(ws / A.PER_ACHIEVEMENT_CSV)
    assert tuple(per[0]) == A.PER_ACHIEVEMENT_FIELDS
    assert [(r["achievement_api_name"], r["global_unlock_pct"]) for r in per] == [
        ("ACH_1", "71.2"), ("ACH_2", "9.5"), ("ACH_3", "0.8"), ("ACH_4", "0.1")]


def test_schema_with_key(ws, legacy_csv):
    adapter = SampleSteam()
    assert ach(ws, legacy_csv, adapter, "--scope", "new", key=FAKE_KEY) == 0
    f = {int(r["appid"]): r for r in read(ws / A.FEATURES_CSV)}
    assert (f[9900001]["achievement_count"], f[9900001]["achievement_count_source"]) == ("4", "schema")
    assert (f[9900003]["has_steam_achievements"], f[9900003]["achievement_count_source"]) == ("FALSE", "schema")
    per = [r for r in read(ws / A.PER_ACHIEVEMENT_CSV) if r["appid"] == "9900001"]
    assert [(r["display_name"], r["hidden"], r["in_schema"]) for r in per][2] == ("Feat 3", "TRUE", "TRUE")
    for path in (ws / A.FEATURES_CSV, ws / A.PER_ACHIEVEMENT_CSV, ws / "work" / "achievements" / "achievement_sources.csv",
                 ws / "reports" / "achievements_coverage.md"):
        assert FAKE_KEY not in path.read_text()
    assert all(q.get("key") == FAKE_KEY for q in calls(adapter, "ISteamUserStats"))  # key goes to the Web API host only
    assert all("key" not in q for q in calls(adapter, "/api/appdetails"))


def test_failures_are_retried_and_resolve(ws, legacy_csv):
    adapter = SampleSteam()
    ach(ws, legacy_csv, adapter, "--scope", "new")
    adapter.calls.clear()
    adapter.percentages[9900004] = {"achievementpercentages": {"achievements": [{"name": "X", "percent": 42}]}}
    assert ach(ws, legacy_csv, adapter, "--scope", "new") == 0
    assert [q["gameid"] for q in calls(adapter, "GetGlobalAchievement")] == ["9900004"]  # only the failed one
    f = {int(r["appid"]): r for r in read(ws / A.FEATURES_CSV)}
    assert (f[9900004]["global_unlock_percentages_available"], f[9900004]["achievement_count"]) == ("TRUE", "1")


def test_rate_limit_stops_and_resumes(ws, legacy_csv):
    adapter = SampleSteam()
    adapter.percentages[9900002] = 429
    assert ach(ws, legacy_csv, adapter, "--scope", "new") == cli.EXIT_TEMPFAIL
    collected = {r["appid"] for r in read(ws / A.FEATURES_CSV)}
    assert "9900002" not in collected  # throttled games are pending, not recorded
    adapter.percentages[9900002] = 403
    assert ach(ws, legacy_csv, adapter, "--scope", "new") == 0
    assert "9900002" in {r["appid"] for r in read(ws / A.FEATURES_CSV)}


def test_interrupted_collection_resumes(ws, legacy_csv):
    adapter = SampleSteam()
    adapter.percentages[9900003] = KeyboardInterrupt()
    assert ach(ws, legacy_csv, adapter, "--scope", "new") == cli.EXIT_INTERRUPTED
    adapter.percentages[9900003] = {"achievementpercentages": {"achievements": []}}
    adapter.calls.clear()
    assert ach(ws, legacy_csv, adapter, "--scope", "new") == 0
    assert sorted(q["gameid"] for q in calls(adapter, "GetGlobalAchievement")) == ["9900003", "9900004"]
    assert len(read(ws / A.FEATURES_CSV)) == 4


def test_legacy_metadata_outputs_are_untouched(ws, legacy_csv):
    before = {n: (ws / n).read_bytes() for n in (pipeline.NEW_CSV, pipeline.MERGED_CSV, pipeline.PROVENANCE_CSV)}
    legacy_hash = hashlib.sha256(legacy_csv.read_bytes()).hexdigest()
    assert ach(ws, legacy_csv, SampleSteam(), "--scope", "new") == 0
    assert {n: (ws / n).read_bytes() for n in before} == before
    assert hashlib.sha256(legacy_csv.read_bytes()).hexdigest() == legacy_hash == pipeline.LEGACY_SHA256
    assert cli.main(["validate", "--base-dir", str(ws), "--work-dir", str(ws / "work"), "--legacy-csv",
                     str(legacy_csv)], environ={}) == 0


def test_explicit_appids_and_export_accumulates(ws, legacy_csv):
    adapter = SampleSteam(appdetails={976730: {"type": "game", "name": "Halo", "achievements": {"total": 700}}})
    adapter.percentages[976730] = {"achievementpercentages": {"achievements": [{"name": "H1", "percent": 3}]}}
    assert ach(ws, legacy_csv, adapter, "--scope", "new") == 0
    assert ach(ws, legacy_csv, adapter, "--appid", "976730") == 0
    ids = [int(r["appid"]) for r in read(ws / A.FEATURES_CSV)]
    assert ids == sorted(ids) and 976730 in ids and 9900001 in ids  # earlier scope is kept
    report = (ws / "reports" / "achievements_coverage.md").read_text()
    assert "observed rarity" in report and "not** a measure of intrinsic difficulty" in report
    assert "## Original games (steamproducts2020.csv)" in report and "Collected 1 of 23034" in report


def test_dry_run_makes_no_requests(ws, legacy_csv, capsys):
    adapter = SampleSteam()
    assert ach(ws, legacy_csv, adapter, "--scope", "legacy", "--dry-run", "--limit", "10") == 0
    assert adapter.calls == [] and not (ws / A.FEATURES_CSV).exists()
    assert "23034 game(s), 23034 to collect, 10 this time" in capsys.readouterr().out

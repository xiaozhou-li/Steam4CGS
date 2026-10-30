"""Monthly snapshots: derivation, idempotency, month guards, caching, audit trail and exports."""

from __future__ import annotations

import csv
import datetime as dt
import json
import shutil

import pytest
from fakes import FAKE_KEY, make_transport
from sample_data import APPDETAILS, REVIEWS, SampleSteam

from crawler import cli, pipeline
from crawler import snapshots as S
from crawler.cache import ResponseCache
from crawler.credentials import ApiKey
from crawler.steam_api import SteamStoreAPI, SteamWebAPI
from crawler.transport import RetryPolicy

UTC = dt.timezone.utc


class Clock:
    def __init__(self, *args):
        self.t = dt.datetime(*args, tzinfo=UTC) if args else dt.datetime(2026, 10, 30, 12, tzinfo=UTC)

    def now(self):
        self.t += dt.timedelta(seconds=1)
        return self.t


def build(adapter, state, clock, *, key=None, cache=None):
    transport = make_transport(adapter, retry=RetryPolicy(max_attempts=1))
    store = SteamStoreAPI(transport, cache=cache, now=clock.now)
    return S.SnapshotCollector(state, SteamWebAPI(transport, ApiKey(key) if key else None), store,
                               now=clock.now, log=lambda _: None)


@pytest.fixture
def state(tmp_path):
    with S.SnapshotState(tmp_path / "snap.sqlite3") as st:
        yield st


def records(state, month=None):
    return {(r["month"], r["appid"]): r for r in state.records(month)}


def calls(adapter, fragment):
    return [q for p, q in adapter.calls if fragment in p]


def healthy():
    """All sample apps answer every source (9900004's store item and 9900005's reviews fixed)."""
    adapter = SampleSteam()
    adapter.tags[9900004] = [{"tagid": 699, "weight": 5}]
    adapter.reviews[9900005] = {"query_summary": {"review_score_desc": "No user reviews", "total_positive": 0,
                                                  "total_negative": 0, "total_reviews": 0}}
    return adapter


# --- pure helpers ----------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "soon", "at", "months"),
    [("14 Mar, 2023", False, dt.date(2026, 10, 30), 43), ("Mar 14, 2023", False, dt.date(2026, 10, 13), 42),
     ("30 Oct, 2026", False, dt.date(2026, 10, 30), 0), ("31 Oct, 2026", False, dt.date(2026, 10, 30), None),
     ("Q3 2022", False, dt.date(2026, 10, 30), None), ("2022", False, dt.date(2026, 10, 30), None),
     ("14 Mar, 2023", True, dt.date(2026, 10, 30), None), ("Coming soon", None, dt.date(2026, 10, 30), None),
     (None, None, dt.date(2026, 10, 30), None)],
)
def test_age_in_months(raw, soon, at, months):
    assert S.age_in_months(raw, soon, at) == months


def obs(source, status, raw=None):
    return S.Observation(1, "2026-10", 10, source, status, f"https://x/{source}", "2026-10-30T12:00:00Z", raw, "")


def derive(**sources):
    defaults = {"appdetails": obs("appdetails", S.OK, {"type": "game", "release_date": {"date": "1 Jan, 2024"},
                                                       "genres": [{"description": "Action"}]}),
                "store_item": obs("store_item", S.OK, {"appid": 10, "is_early_access": False}),
                "current_players": obs("current_players", S.OK, {"player_count": 5, "result": 1}),
                "reviews": obs("reviews", S.NOT_ATTEMPTED)}
    defaults.update(sources)
    return S.derive_record(10, defaults, dt.date(2026, 10, 30))


@pytest.mark.parametrize(
    ("data", "cents", "status"),
    [({"is_free": True}, "0", S.OK),
     ({"price_overview": {"currency": "USD", "initial": 1999, "final": 499, "discount_percent": 75}}, "1999", S.OK),
     ({"price_overview": {"currency": "EUR", "initial": 1999}}, "", S.UNRECOGNIZED),
     ({}, "", S.MISSING)],
)
def test_price_is_the_regular_price_in_cents(data, cents, status):
    values, statuses = derive(appdetails=obs("appdetails", S.OK, {"type": "game", **data}))
    assert (values["price_usd_cents"], statuses["price_usd_cents"]) == (cents, status)


@pytest.mark.parametrize(
    ("data", "count", "status"),
    [({"achievements": {"total": 40}}, "40", S.OK), ({"achievements": {"total": 0}}, "0", S.OK),
     ({}, "", S.MISSING), ({"categories": [{"description": "Steam Achievements"}]}, "", S.MISSING),
     ({"achievements": {"total": "x"}}, "", S.UNRECOGNIZED)],
)
def test_achievement_count(data, count, status):
    values, statuses = derive(appdetails=obs("appdetails", S.OK, {"type": "game", **data}))
    assert (values["achievement_count"], statuses["achievement_count"]) == (count, status)


def test_reviews_share_and_never_a_recent_count():
    raw = {"status": "ok", "total_reviews": 1234, "total_positive": 1100, "total_negative": 134}
    values, statuses = derive(reviews=obs("reviews", S.OK, raw))
    assert (values["review_total"], values["review_positive"], values["review_negative"],
            values["review_positive_share"]) == ("1234", "1100", "134", "0.8914")
    assert values["recent_review_count"] == "" and statuses["recent_review_count"] == S.NOT_AVAILABLE
    zero = derive(reviews=obs("reviews", S.OK, {"total_reviews": 0, "total_positive": 0, "total_negative": 0}))[0]
    assert (zero["review_total"], zero["review_positive_share"]) == ("0", "")


@pytest.mark.parametrize("status", [S.NOT_ATTEMPTED, S.UNRECOGNIZED, S.ERROR, S.NOT_AVAILABLE])
def test_unavailable_reviews_stay_blank(status):
    values, statuses = derive(reviews=obs("reviews", status))
    assert all(values[k] == "" for k in ("review_total", "review_positive", "review_negative", "review_positive_share"))
    assert statuses["reviews"] == status


def test_zero_players_is_a_value_and_unavailable_is_blank():
    assert derive(current_players=obs("current_players", S.OK, {"player_count": 0, "result": 1}))[0][
        "current_player_count"] == "0"
    values, statuses = derive(current_players=obs("current_players", S.NOT_AVAILABLE, {"result": 42}))
    assert (values["current_player_count"], statuses["current_player_count"]) == ("", S.NOT_AVAILABLE)


@pytest.mark.parametrize(
    ("item", "genres", "value"),
    [({"is_early_access": True}, ["Action", "Early Access"], "TRUE"), ({"is_early_access": False}, ["Action"], "FALSE"),
     ({}, ["Action"], ""), ({}, ["Early Access"], "TRUE")],
)
def test_early_access_current(item, genres, value):
    ad = obs("appdetails", S.OK, {"type": "game", "genres": [{"description": g} for g in genres]})
    assert derive(appdetails=ad, store_item=obs("store_item", S.OK, {"appid": 10, **item}))[0]["early_access_current"] == value


# --- collection --------------------------------------------------------------------------------------

def test_first_snapshot_values(state):
    adapter = SampleSteam()
    report = build(adapter, state, Clock()).run(sorted(APPDETAILS), month="2026-10")
    r = {appid: rec for (_, appid), rec in records(state).items()}
    v1 = r[9900001]["values"]
    assert (v1["age_months"], v1["review_total"], v1["review_positive_share"], v1["current_player_count"],
            v1["achievement_count"], v1["price_usd_cents"], v1["early_access_current"]) == \
        ("43", "1234", "0.8914", "1234", "40", "1999", "FALSE")
    v2 = r[9900002]["values"]
    assert (v2["current_player_count"], v2["price_usd_cents"], v2["early_access_current"], v2["review_total"]) == \
        ("0", "0", "TRUE", "3")
    v3, s3 = r[9900003]["values"], r[9900003]["statuses"]
    assert (v3["current_player_count"], s3["current_player_count"], v3["age_months"], s3["age_months"]) == \
        ("", S.NOT_AVAILABLE, "", S.MISSING)
    assert r[9900004]["statuses"]["reviews"] == S.UNRECOGNIZED  # inconsistent summary is not used
    assert not r[9900004]["complete"] and not r[9900005]["complete"]  # store item 500 / reviews 503
    assert report.pending == 2 and report.collected == 5
    assert all(rec["snapshot_utc"].startswith("2026-10") for rec in r.values())


def test_rerun_is_idempotent_and_makes_no_requests(state, tmp_path):
    adapter = healthy()
    c = build(adapter, state, Clock())
    c.run(sorted(APPDETAILS), month="2026-10")
    S.export(state, tmp_path, tmp_path)
    first = {n: (tmp_path / n).read_bytes() for n in (S.SNAPSHOT_CSV, S.ANALYSIS_CSV)}
    adapter.calls.clear()
    report = c.run(sorted(APPDETAILS), month="2026-10")
    assert adapter.calls == [] and report.collected == 0 and report.pending == 0
    S.export(state, tmp_path, tmp_path)
    assert {n: (tmp_path / n).read_bytes() for n in first} == first
    assert len(state.records("2026-10")) == len(APPDETAILS)


def test_retry_refetches_only_failed_sources(state):
    adapter = SampleSteam()
    c = build(adapter, state, Clock())
    c.run([9900004], month="2026-10")
    adapter.tags[9900004] = [{"tagid": 699, "weight": 5}]
    adapter.calls.clear()
    report = c.run([9900004], month="2026-10")
    assert [p for p, _ in adapter.calls if "Supported" not in p] == ["/IStoreBrowseService/GetItems/v1/"]
    assert report.pending == 0 and state.records("2026-10")[0]["complete"]


@pytest.mark.parametrize("month", ["2026-09", "2025-10", "2026-11"])
def test_only_the_current_month_can_be_collected(state, month):
    adapter = SampleSteam()
    with pytest.raises(S.SnapshotMonthError, match="current UTC month"):
        build(adapter, state, Clock()).run([9900001], month=month)
    assert adapter.calls == [] and state.records() == []


def test_new_month_creates_new_records_and_ignores_last_months_cache(state, tmp_path):
    adapter = healthy()
    with ResponseCache(tmp_path / "cache.sqlite3") as cache:
        build(adapter, state, Clock(2026, 10, 31, 20), cache=cache).run([9900001], month="2026-10")
        adapter.reviews[9900001] = {"query_summary": {"review_score_desc": "Very Positive", "total_positive": 1180,
                                                      "total_negative": 140, "total_reviews": 1320}}
        adapter.players[9900001] = 999
        adapter.calls.clear()
        build(adapter, state, Clock(2026, 11, 1, 1), cache=cache).run([9900001], month="2026-11")
    assert len(calls(adapter, "/api/appdetails")) == 1  # the October cache entry was not reused for November
    S.export(state, tmp_path, tmp_path, new_ids=[9900001])
    with open(tmp_path / S.ANALYSIS_CSV, newline="", encoding="utf-8") as f:
        panel = list(csv.DictReader(f))
    assert [(p["snapshot_month"], p["snapshot_index"], p["months_since_previous_snapshot"], p["review_total"],
             p["review_total_change"], p["current_player_count"]) for p in panel] == [
        ("2026-10", "0", "", "1234", "", "1234"), ("2026-11", "1", "1", "1320", "86", "999")]
    assert {p["dataset"] for p in panel} == {"new"}


def test_gap_months_are_not_filled_in(state, tmp_path):
    adapter = healthy()
    build(adapter, state, Clock(2026, 10, 5)).run([9900001], month="2026-10")
    build(adapter, state, Clock(2026, 12, 5)).run([9900001], month="2026-12")
    S.export(state, tmp_path, tmp_path)
    with open(tmp_path / S.SNAPSHOT_CSV, newline="", encoding="utf-8") as f:
        assert [r["snapshot_month"] for r in csv.DictReader(f)] == ["2026-10", "2026-12"]
    with open(tmp_path / S.ANALYSIS_CSV, newline="", encoding="utf-8") as f:
        assert [r["months_since_previous_snapshot"] for r in csv.DictReader(f)] == ["", "2"]


@pytest.mark.parametrize(("second", "straddles"), [(50, True), (55, True), (58, False)])
def test_month_boundary_during_a_run_stops_cleanly(state, second, straddles):
    clock = Clock(2026, 10, 31, 23, 59, second)
    report = build(healthy(), state, clock).run(sorted(APPDETAILS), month="2026-10")
    assert report.stop_reason == "month_ended" and report.pending > 0
    for r in state.records():
        assert r["month"] == "2026-10" and r["snapshot_utc"].startswith("2026-10")
    # Observations made after midnight are kept in the audit trail with their real time, but unused.
    used = {i for r in state.records() for i in r["obs_ids"]}
    late = [o for o in state.all_observations() if o[6].startswith("2026-11")]
    assert bool(late) is straddles  # 58: midnight passes before the first game, so nothing is fetched
    assert not {o[0] for o in late} & used


def test_rate_limit_stops_then_resumes(state):
    adapter = healthy()
    adapter.players[9900002] = (429,)
    c = build(adapter, state, Clock())
    report = c.run(sorted(APPDETAILS), month="2026-10")
    assert report.stop_reason == "rate_limited" and (("2026-10", 9900002) not in records(state))
    adapter.players[9900002] = 0
    adapter.calls.clear()
    report = c.run(sorted(APPDETAILS), month="2026-10")
    assert report.pending == 0
    # 9900002's appdetails is reused from the audit trail; only games never reached are fetched.
    assert [q["appids"] for q in calls(adapter, "/api/appdetails")] == ["9900003", "9900004", "9900005"]


def test_interruption_keeps_observations(state):
    adapter = healthy()
    adapter.players[9900003] = KeyboardInterrupt()
    c = build(adapter, state, Clock())
    with pytest.raises(KeyboardInterrupt):
        c.run(sorted(APPDETAILS), month="2026-10")
    adapter.players[9900003] = 7
    adapter.calls.clear()
    c.run(sorted(APPDETAILS), month="2026-10")
    assert [q["appid"] for q in calls(adapter, "GetNumberOfCurrentPlayers")] == ["9900003", "9900004", "9900005"]
    assert calls(adapter, "/api/appdetails") == [{"appids": "9900004", "cc": "us", "l": "english"},
                                                 {"appids": "9900005", "cc": "us", "l": "english"}]


def test_refresh_appends_to_the_audit_trail(state, tmp_path):
    adapter = healthy()
    c = build(adapter, state, Clock())
    c.run([9900001], month="2026-10")
    first_ids = state.records("2026-10")[0]["obs_ids"]
    c.run([9900001], month="2026-10", refresh=True)
    second_ids = state.records("2026-10")[0]["obs_ids"]
    assert set(first_ids).isdisjoint(second_ids)
    assert len(list(state.all_observations())) == 8  # 4 sources x 2 collections, nothing deleted


def test_reviews_without_access_are_not_attempted(state):
    adapter = SampleSteam(review_params=None)
    report = build(adapter, state, Clock()).run([9900001], month="2026-10")
    assert not report.reviews_available and calls(adapter, "GetAppReviews") == []
    rec = state.records()[0]
    assert rec["statuses"]["reviews"] == S.NOT_ATTEMPTED and rec["values"]["review_total"] == ""
    assert rec["complete"]  # not attempted is not a failure


# --- exports ---------------------------------------------------------------------------------------------

def test_exports_and_audit(state, tmp_path):
    adapter = SampleSteam()
    build(adapter, state, Clock(), key=FAKE_KEY).run(sorted(APPDETAILS), month="2026-10")
    S.export(state, tmp_path, tmp_path, new_ids=[9900001], legacy_ids=[9900002])
    with open(tmp_path / S.SNAPSHOT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert tuple(reader.fieldnames) == S.SNAPSHOT_FIELDS
        keys = [(r["appid"], r["snapshot_month"]) for r in reader]
    assert len(keys) == len(set(keys)) == 5
    schema_doc = json.loads((tmp_path / S.ANALYSIS_SCHEMA).read_text())
    with open(tmp_path / S.ANALYSIS_CSV, newline="", encoding="utf-8") as f:
        assert tuple(csv.DictReader(f).fieldnames) == tuple(schema_doc["columns"])
    audit = (tmp_path / "snapshots" / "snapshot_audit.csv").read_text()
    assert FAKE_KEY not in audit and FAKE_KEY not in (tmp_path / S.SNAPSHOT_CSV).read_text()
    with open(tmp_path / "snapshots" / "snapshot_audit.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 20 and all(r["url"].startswith("https://") and r["retrieved_at"].startswith("2026-10")
                                   for r in rows)
    referenced = {i for r in state.records() for i in r["obs_ids"]}
    assert referenced <= {int(r["obs_id"]) for r in rows}
    runs = (tmp_path / "snapshots" / "snapshot_runs.csv").read_text()
    assert "2026-10" in runs


def test_analysis_export_loads_with_its_schema(state, tmp_path):
    pd = pytest.importorskip("pandas")
    build(SampleSteam(), state, Clock()).run(sorted(APPDETAILS), month="2026-10")
    S.export(state, tmp_path, tmp_path)
    schema_doc = json.loads((tmp_path / S.ANALYSIS_SCHEMA).read_text())
    dtypes = {k: v["dtype"] for k, v in schema_doc["columns"].items() if not v["dtype"].startswith("datetime")}
    df = pd.read_csv(tmp_path / S.ANALYSIS_CSV, dtype=dtypes, keep_default_na=False, na_values={
        k: [""] for k, t in dtypes.items() if t != "string"}, parse_dates=["snapshot_utc"])
    assert len(df) == 5 and str(df["current_player_count"].dtype) == "Int64"
    assert df.loc[df.appid == 9900001, "review_positive_share"].item() == pytest.approx(0.8914)
    assert df["recent_review_count"].isna().all() and str(df["early_access_current"].dtype) == "boolean"


# --- CLI and isolation -------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def crawled(tmp_path_factory, legacy_csv):
    base = tmp_path_factory.mktemp("snapws")
    argv = ["run", "--base-dir", str(base), "--work-dir", str(base / "work"), "--legacy-csv", str(legacy_csv),
            "--end-date", "2026-10-30"]
    assert cli.main(argv, environ={"STEAM_API_KEY": FAKE_KEY}, transport=make_transport(
        SampleSteam(catalog={a: 1 for a in APPDETAILS}), retry=RetryPolicy(max_attempts=1))) == 0
    return base


def test_cli_snapshot_leaves_the_13_column_data_untouched(crawled, tmp_path, legacy_csv, capsys):
    ws = tmp_path / "ws"
    shutil.copytree(crawled, ws)
    before = {n: (ws / n).read_bytes() for n in (pipeline.NEW_CSV, pipeline.MERGED_CSV)}
    argv = ["snapshot", "--base-dir", str(ws), "--work-dir", str(ws / "work"), "--legacy-csv", str(legacy_csv),
            "--scope", "new"]
    assert cli.main(argv, environ={}, transport=make_transport(healthy(), retry=RetryPolicy(max_attempts=1))) == 0
    assert {n: (ws / n).read_bytes() for n in before} == before
    assert (ws / S.SNAPSHOT_CSV).exists() and (ws / "reports" / "snapshot_coverage.md").exists()
    assert "proxies" in (ws / "reports" / "snapshot_coverage.md").read_text()
    assert cli.main(["validate", "--base-dir", str(ws), "--work-dir", str(ws / "work"), "--legacy-csv",
                     str(legacy_csv)], environ={}) == 0
    adapter = healthy()
    past = ["--month", "2020-01"]
    assert cli.main(argv + past, environ={}, transport=make_transport(adapter)) == cli.EXIT_USAGE
    assert adapter.calls == [] and "can only be collected for the current UTC month" in capsys.readouterr().out
    assert cli.main(argv + ["--export-only", "--month", "2020-01"], environ={}) == 0  # exporting is always allowed

"""Extraction pipeline tests (emulated Steam service) and the committed sample output."""

from __future__ import annotations

import csv
from urllib.parse import parse_qs, urlsplit

import pytest
from fakes import FAKE_KEY
from sample_data import APPDETAILS, SAMPLES_DIR, SampleSteam, build_extractor, write_samples

from crawler import cli, extract, schema
from crawler import metadata as m
from crawler.credentials import ApiKey
from crawler.extract import ExtractionState


@pytest.fixture
def state(tmp_path):
    with ExtractionState(tmp_path / "x.sqlite3") as s:
        yield s


def read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_end_to_end_outputs(state, tmp_path):
    adapter = SampleSteam()
    report = build_extractor(state, adapter).run(sorted(APPDETAILS))
    counts = extract.export(state, tmp_path / "out")
    assert counts == {"games": 4, "provenance_rows": 52, "failed": 1}
    assert (report.extracted, report.partial, report.failed, report.pending) == (3, 1, 1, 0)

    assert schema.validate_file(tmp_path / "out" / "games.csv") == []
    games = {int(r["id"]): r for r in read(tmp_path / "out" / "games.csv")}
    assert games[9900001] == {
        "id": "9900001", "title": "Sample: Kestrel &amp; Crow", "genres": "['Action', 'Adventure', 'Indie']",
        "developer": "Sample Studio, Port House (Mac)", "publisher": "Example Publishing",
        "release_date": "14 Mar, 2023", "app_name": "Sample: Kestrel & Crow",
        "specs": "['Single-player', 'Multi-player', 'Steam Achievements', 'Full controller support', 'Steam Cloud', 'Family Sharing']",
        "tags": "['Indie', 'Action', 'Point & Click', 'Adventure']", "price": "20", "sentiment": "Very Positive",
        "early_access": "FALSE", "metascore": "84",
    }
    assert (games[9900002]["price"], games[9900002]["early_access"], games[9900002]["sentiment"],
            games[9900002]["metascore"]) == ("0", "TRUE", "3 user reviews", "")
    sparse = games[9900003]
    assert (sparse["genres"], sparse["developer"], sparse["specs"], sparse["tags"], sparse["price"],
            sparse["early_access"], sparse["sentiment"], sparse["release_date"]) == ("", "", "", "", "", "", "", "Q3 2022")
    assert games[9900004]["tags"] == ""  # tags request failed; genres not substituted
    assert games[9900004]["genres"] == "['Casual', 'Racing']"

    (failure,) = read(tmp_path / "out" / "failed.csv")
    assert (failure["appid"], failure["error"]) == ("9900005", "store_details_unavailable")
    provenance = read(tmp_path / "out" / "provenance.csv")
    assert tuple(provenance[0]) == m.PROVENANCE_FIELDS
    assert {(r["appid"], r["field"]) for r in provenance} == {(str(a), c) for a in games for c in schema.COLUMNS}

    prices = {int(r["appid"]): r for r in read(tmp_path / "out" / "prices.csv")}
    assert tuple(next(iter(prices.values()))) == extract.PRICE_FIELDS
    assert {k: prices[9900001][k] for k in ("price", "currency", "initial_cents", "final_cents", "discount_percent",
                                             "is_free", "status")} == {
        "price": "20", "currency": "USD", "initial_cents": "1999", "final_cents": "1499", "discount_percent": "25",
        "is_free": "false", "status": "present"}
    assert (prices[9900002]["price"], prices[9900002]["is_free"], prices[9900002]["currency"]) == ("0", "true", "")
    assert (prices[9900003]["price"], prices[9900003]["status"]) == ("", "missing")


def test_requests_per_app_and_single_tag_list(state):
    adapter = SampleSteam()
    build_extractor(state, adapter).run([9900001, 9900002])
    paths = [p for p, _ in adapter.calls]
    assert paths.count("/api/appdetails") == 2
    assert paths.count("/IStoreService/GetTagList/v1/") == 1
    assert paths.count("/IStoreBrowseService/GetItems/v1/") == 2
    appdetails = [q for p, q in adapter.calls if p == "/api/appdetails"]
    assert all(q["cc"] == "us" and q["l"] == "english" for q in appdetails)
    reviews = [q for p, q in adapter.calls if p.startswith("/appreviews/")]
    assert all(q == {"json": "1", "language": "all", "purchase_type": "all", "review_type": "all",
                     "num_per_page": "0"} for q in reviews)


def test_failed_appdetails_skips_other_sources(state):
    adapter = SampleSteam()
    build_extractor(state, adapter).run([9900005])
    assert [p for p, _ in adapter.calls] == ["/api/appdetails"]


def test_legacy_appids_are_refused(state):
    adapter = SampleSteam()
    report = build_extractor(state, adapter, legacy_ids={9900001}).run([9900001, 9900002])
    assert report.skipped_legacy == [9900001]
    assert all(q.get("appids") != "9900001" for p, q in adapter.calls if p == "/api/appdetails")


def test_limit_resume_and_partial_retry(state):
    adapter = SampleSteam()
    ex = build_extractor(state, adapter)
    report = ex.run(sorted(APPDETAILS), limit=2)
    assert report.stop_reason == "limit_reached" and report.attempted == 2 and report.pending == 3

    adapter.calls.clear()
    build_extractor(state, adapter).run(sorted(APPDETAILS))
    fetched = [int(q["appids"]) for p, q in adapter.calls if p == "/api/appdetails"]
    assert fetched == [9900003, 9900004, 9900005]  # completed apps are not refetched

    adapter.calls.clear()
    adapter.tags[9900004] = [{"tagid": 699, "weight": 5}]
    build_extractor(state, adapter).run(sorted(APPDETAILS))
    fetched = [int(q["appids"]) for p, q in adapter.calls if p == "/api/appdetails"]
    assert fetched == [9900004, 9900005]  # partial (tags failed) and failed apps are retried
    rows = {int(r["id"]): r for r, _ in state.rows()}
    assert rows[9900004]["tags"] == "['Racing']"


def test_refresh_refetches_everything(state):
    adapter = SampleSteam()
    build_extractor(state, adapter).run([9900001])
    adapter.calls.clear()
    build_extractor(state, adapter).run([9900001], refresh=True)
    assert [p for p, _ in adapter.calls if p == "/api/appdetails"] == ["/api/appdetails"]


def test_tag_list_failure_marks_tags_unavailable(state):
    adapter = SampleSteam(tag_names=503)
    build_extractor(state, adapter).run([9900001])
    (row, provenance), = state.rows()
    assert row["tags"] == ""
    tags = next(p for p in provenance if p["field"] == "tags")
    assert tags["status"] == m.SOURCE_UNAVAILABLE and "GetTagList" in tags["note"]


def test_api_key_never_reaches_provenance(state, tmp_path):
    adapter = SampleSteam()
    build_extractor(state, adapter, key=ApiKey(FAKE_KEY)).run([9900001])
    extract.export(state, tmp_path / "out")
    text = (tmp_path / "out" / "provenance.csv").read_text(encoding="utf-8")
    assert FAKE_KEY not in text and "key=" not in text
    items = [q for p, q in adapter.calls if p == "/IStoreBrowseService/GetItems/v1/"]
    assert items[0]["key"] == FAKE_KEY  # the key is sent to the Web API host when configured...
    stores = [q for p, q in adapter.calls if p == "/api/appdetails" or p.startswith("/appreviews/")]
    assert all("key" not in q for q in stores)  # ...and never to the store host


def test_provenance_source_urls_are_reproducible(state):
    build_extractor(state, SampleSteam()).run([9900001])
    (_, provenance), = state.rows()
    urls = {p["field"]: p["source_url"] for p in provenance}
    assert urls["price"] == "https://store.steampowered.com/api/appdetails?appids=9900001&cc=us&l=english"
    assert urls["sentiment"].startswith("https://store.steampowered.com/appreviews/9900001?json=1")
    tag_query = parse_qs(urlsplit(urls["tags"]).query)
    assert '"appid":9900001' in tag_query["input_json"][0]


def test_cli_extract(tmp_path, capsys):
    from fakes import make_transport
    from crawler.transport import RetryPolicy

    eligible = tmp_path / "eligible.csv"
    eligible.write_text("appid,name\r\n9900002,Sample Sandbox\r\n9900001,Sample\r\n9900002,dup\r\n", encoding="utf-8")
    args = cli.build_parser().parse_args(["extract", "--input", str(eligible), "--state", str(tmp_path / "s.sqlite3"),
                                          "--out", str(tmp_path / "out")])
    transport = make_transport(SampleSteam(), retry=RetryPolicy(max_attempts=1))
    assert cli.run_extract(args, {}, transport=transport) == 0
    out = capsys.readouterr().out
    assert "games=2" in out and "provenance_rows=26" in out
    assert [r["id"] for r in read(tmp_path / "out" / "games.csv")] == ["9900001", "9900002"]


def test_cli_extract_missing_input(tmp_path, capsys):
    args = cli.build_parser().parse_args(["extract", "--input", str(tmp_path / "nope.csv"),
                                          "--state", str(tmp_path / "s"), "--out", str(tmp_path / "o")])
    assert cli.run_extract(args, {}) == cli.EXIT_USAGE


def test_committed_samples_are_up_to_date(tmp_path):
    out = tmp_path / "samples"
    out.mkdir()
    write_samples(out, tmp_path / "state.sqlite3")
    for name in ("games.sample.csv", "provenance.sample.csv", "prices.sample.csv", "failed.sample.csv"):
        assert (out / name).read_bytes() == (SAMPLES_DIR / name).read_bytes(), (
            f"docs/samples/{name} is stale; regenerate with: python3.13 tests/sample_data.py")

"""SteamSpy tags: parsing, deterministic ordering, caching, rate limiting, and unavailable responses."""

from __future__ import annotations

import csv
import datetime as dt
import random

import pytest
import requests
from fakes import FAKE_KEY, FakeClock, FakeSteam, make_transport, reply
from sample_data import APPDETAILS, FIXED_NOW, STEAMSPY, SampleSteam, build_extractor

from crawler import cli, extract, schema
from crawler import metadata as m
from crawler import steamspy as spy
from crawler.credentials import ApiKey
from crawler.extract import ExtractionState
from crawler.transport import DEFAULT_MIN_INTERVALS, SteamError, SteamResponseError, SteamTransport


def body(appid=10, tags=None, name="Game"):
    return {"appid": appid, "name": name, "tags": {} if tags is None else tags}


def read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def spy_calls(adapter):
    return [q for p, q in adapter.calls if p == "/api.php"]


# --- parsing ------------------------------------------------------------------------------

def test_tags_sorted_by_votes_then_name():
    tags = {"b": 5, "A": 5, "a": 5, "Zed": 9, "c": 1}
    assert spy.parse_tags(10, body(tags=tags)) == [("Zed", 9), ("A", 5), ("a", 5), ("b", 5), ("c", 1)]


def test_order_is_independent_of_response_order():
    tags = {f"T{i}": i % 4 for i in range(40)}
    expected = spy.parse_tags(10, body(tags=tags))
    rng = random.Random(7)
    for _ in range(20):
        items = list(tags.items())
        rng.shuffle(items)
        assert spy.parse_tags(10, body(tags=dict(items))) == expected


def test_string_appid_and_whitespace_names():
    assert spy.parse_tags(10, {"appid": "10", "name": "G", "tags": {" Indie ": 3}}) == [("Indie", 3)]


@pytest.mark.parametrize("tags", [[], {}, None])
def test_empty_tags(tags):
    assert spy.parse_tags(10, {"appid": 10, "name": "G", "tags": tags}) == []


@pytest.mark.parametrize("name", [None, ""])
def test_no_record(name):
    assert spy.parse_tags(10, {"appid": 10, "name": name, "tags": []}) is None


@pytest.mark.parametrize(
    "data",
    [None, [], "x", body(appid=11), {"name": "G", "tags": {}},
     body(tags=["Indie"]), body(tags={"Indie": "3"}), body(tags={"Indie": -1}), body(tags={"Indie": True}),
     body(tags={"Indie": 3.5}), body(tags={"": 3}), body(tags={"In\ndie": 3}), body(tags={"Indie": 1, " Indie": 2})],
)
def test_malformed_responses_raise(data):
    with pytest.raises(spy.SteamSpyFormatError):
        spy.parse_tags(10, data)


# --- client: caching and rate limits ---------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = FIXED_NOW

    def __call__(self):
        return self.t


def client(fake, tmp_path, now, **kwargs):
    cache = spy.SteamSpyCache(tmp_path / "spy.sqlite3")
    return spy.SteamSpyClient(make_transport(fake), cache, now=now, **kwargs), cache


def test_request_shape_and_no_key(tmp_path):
    fake = FakeSteam(reply(body=body()))
    c, _ = client(fake, tmp_path, Clock())
    r = c.appdetails(10)
    (call,) = fake.calls
    assert call.host == "steamspy.com" and call.path == "/api.php"
    assert call.params == {"request": "appdetails", "appid": "10"}
    assert r.url == "https://steamspy.com/api.php?request=appdetails&appid=10" and not r.from_cache


def test_cache_hit_within_ttl_keeps_original_time(tmp_path):
    now = Clock()
    fake = FakeSteam(reply(body=body(tags={"Indie": 1})))
    c, _ = client(fake, tmp_path, now)
    first = c.appdetails(10)
    now.t += dt.timedelta(hours=23)
    second = c.appdetails(10)
    assert len(fake.calls) == 1 and second.from_cache and second.data == first.data
    assert second.retrieved_at == first.retrieved_at == "2026-10-30T12:00:00Z"
    assert c.network_requests == 1


def test_cache_expires_after_ttl(tmp_path):
    now = Clock()
    fake = FakeSteam(reply(body=body(tags={"Indie": 1})), reply(body=body(tags={"Indie": 2})))
    c, _ = client(fake, tmp_path, now, ttl=dt.timedelta(hours=24))
    c.appdetails(10)
    now.t += dt.timedelta(hours=24)
    r = c.appdetails(10)
    assert len(fake.calls) == 2 and not r.from_cache and r.data["tags"] == {"Indie": 2}


def test_cache_persists_across_clients(tmp_path):
    now = Clock()
    c1, cache = client(FakeSteam(reply(body=body())), tmp_path, now)
    c1.appdetails(10)
    cache.close()
    fake2 = FakeSteam()
    c2, _ = client(fake2, tmp_path, now)
    assert c2.appdetails(10).from_cache and fake2.calls == []


def test_failures_are_not_cached(tmp_path):
    now = Clock()
    fake = FakeSteam(reply(500, {}), reply(500, {}), reply(500, {}), reply(body=body()))
    c, cache = client(fake, tmp_path, now)
    with pytest.raises(SteamError):
        c.appdetails(10)
    assert cache.get(10) is None
    assert not c.appdetails(10).from_cache


@pytest.mark.parametrize("payload", [reply(body=[1, 2]), reply(body=None),
                                     reply(200, text="<html>busy</html>", headers={"Content-Type": "text/html"})])
def test_non_object_bodies_raise_and_are_not_cached(tmp_path, payload):
    c, cache = client(FakeSteam(payload), tmp_path, Clock())
    with pytest.raises(SteamResponseError):
        c.appdetails(10)
    assert cache.get(10) is None


def test_requests_are_spaced_at_least_one_second():
    assert DEFAULT_MIN_INTERVALS["steamspy.com"] >= 1.0
    clock = FakeClock()
    fake = FakeSteam(*[reply(body=body(appid=a)) for a in (1, 2, 3)])
    session = requests.Session()
    session.mount("https://", fake)
    transport = SteamTransport(session=session, sleep=clock.sleep, clock=clock.time, wall_clock=clock.time)
    c = spy.SteamSpyClient(transport, None)
    for a in (1, 2, 3):
        c.appdetails(a)
    assert clock.sleeps == [DEFAULT_MIN_INTERVALS["steamspy.com"]] * 2


def test_retry_after_is_honoured(tmp_path):
    clock = FakeClock()
    fake = FakeSteam(reply(429, {}, headers={"Retry-After": "30"}), reply(body=body()))
    c = spy.SteamSpyClient(make_transport(fake, clock), None)
    c.appdetails(10)
    assert 30.0 in clock.sleeps


# --- extraction with --tags-source steamspy ---------------------------------------------------------

@pytest.fixture
def run_spy(tmp_path):
    def run(adapter=None, appids=sorted(APPDETAILS), **kwargs):
        adapter = adapter or SampleSteam()
        with ExtractionState(tmp_path / "x.sqlite3") as state, spy.SteamSpyCache(tmp_path / "spy.sqlite3") as cache:
            report = build_extractor(state, adapter, tags_source="steamspy", steamspy_cache=cache, **kwargs).run(appids)
            extract.export(state, tmp_path / "out")
        games = {int(r["id"]): r for r in read(tmp_path / "out" / "games.csv")}
        prov = {(int(r["appid"]), r["field"]): r for r in read(tmp_path / "out" / "provenance.csv")}
        votes = read(tmp_path / "out" / "tag_votes.csv")
        return adapter, report, games, prov, votes
    return run


def test_top_twenty_in_legacy_list_format(run_spy):
    _, _, games, prov, votes = run_spy()
    expected = [n for n, _ in spy.parse_tags(9900001, STEAMSPY[9900001])[:20]]
    assert games[9900001]["tags"] == schema.encode_list(expected)
    assert expected[:5] == ["Action", "Indie", "Point & Click", "Adventure", "adventure"]  # ties by name
    assert len(expected) == 20 and "Sample Tag 17" not in expected
    p = prov[(9900001, "tags")]
    assert p["status"] == m.PRESENT and p["source"] == "steamspy:tags" and "top 20 of 23" in p["note"]
    assert p["source_url"] == "https://steamspy.com/api.php?request=appdetails&appid=9900001"
    assert p["retrieved_at"] == "2026-10-30T12:00:00Z"
    rows = [v for v in votes if v["appid"] == "9900001"]
    assert [(v["rank"], v["tag"], v["count"]) for v in rows[:3]] == [("1", "Action", "120"), ("2", "Indie", "120"),
                                                                     ("3", "Point & Click", "80")]
    assert len(rows) == 20 and {v["count_kind"] for v in rows} == {"steamspy_votes"}


def test_unavailable_or_empty_steamspy_leaves_tags_blank(run_spy):
    _, report, games, prov, votes = run_spy()
    assert games[9900002]["tags"] == "" and prov[(9900002, "tags")]["status"] == m.MISSING
    assert "SteamSpy lists no tags" in prov[(9900002, "tags")]["note"]
    assert games[9900003]["tags"] == "" and "no record" in prov[(9900003, "tags")]["note"]
    assert games[9900004]["tags"] == "" and prov[(9900004, "tags")]["status"] == m.SOURCE_UNAVAILABLE
    assert "HTTP 500" in prov[(9900004, "tags")]["note"]
    for appid in (9900002, 9900004):
        assert games[appid]["genres"]  # genres exist, and are not substituted
    assert {v["appid"] for v in votes} == {"9900001"}
    assert report.partial == 1  # 9900004: SteamSpy failed, retried next run


@pytest.mark.parametrize(
    "steamspy_body",
    [{"appid": 9900001, "name": "x", "tags": {"Indie": "many"}},
     {"appid": 1, "name": "x", "tags": {"Indie": 1}},
     ["not", "an", "object"]],
)
def test_malformed_steamspy_is_unrecognized_not_guessed(run_spy, steamspy_body):
    adapter = SampleSteam(steamspy={9900001: steamspy_body})
    _, _, games, prov, _ = run_spy(adapter, [9900001])
    assert games[9900001]["tags"] == ""
    assert prov[(9900001, "tags")]["status"] in (m.UNRECOGNIZED, m.SOURCE_UNAVAILABLE)


def test_html_error_page_is_source_unavailable(run_spy):
    class HtmlSpy(SampleSteam):
        def send(self, request, **kwargs):
            if "steamspy.com" in request.url:
                self.calls.append(("/api.php", {}))
                resp = requests.Response()
                resp.status_code, resp._content, resp._content_consumed = 200, b"<html>down</html>", True
                resp.headers = requests.structures.CaseInsensitiveDict({"Content-Type": "text/html"})
                resp.url, resp.request = request.url, request
                return resp
            return super().send(request, **kwargs)

    _, _, games, prov, _ = run_spy(HtmlSpy(), [9900001])
    assert games[9900001]["tags"] == "" and prov[(9900001, "tags")]["status"] == m.SOURCE_UNAVAILABLE
    assert "non-JSON" in prov[(9900001, "tags")]["note"]


def test_steamspy_rate_limit_stops_the_run(run_spy):
    adapter = SampleSteam(steamspy={9900001: 429})
    _, report, games, _, _ = run_spy(adapter, [9900001, 9900002])
    assert report.stop_reason == "rate_limited" and games == {}


def test_cached_second_run_makes_no_steamspy_requests(tmp_path, run_spy):
    adapter, _, _, _, _ = run_spy()
    first = len(spy_calls(adapter))
    adapter.calls.clear()
    with ExtractionState(tmp_path / "x.sqlite3") as state, spy.SteamSpyCache(tmp_path / "spy.sqlite3") as cache:
        build_extractor(state, adapter, tags_source="steamspy", steamspy_cache=cache).run(sorted(APPDETAILS),
                                                                                         refresh=True)
        prov = {(int(r["appid"]), r["field"]): r for _, rows in state.rows() for r in rows}
    assert first == 4 and len(spy_calls(adapter)) == 1  # only 9900004 (its failure was not cached)
    assert "served from cache" in prov[(9900001, "tags")]["note"]


def test_switching_tag_source_reextracts(tmp_path):
    adapter = SampleSteam()
    with ExtractionState(tmp_path / "x.sqlite3") as state, spy.SteamSpyCache(tmp_path / "spy.sqlite3") as cache:
        build_extractor(state, adapter).run([9900001])
        assert spy_calls(adapter) == []
        build_extractor(state, adapter, tags_source="steamspy", steamspy_cache=cache).run([9900001])
        assert len(spy_calls(adapter)) == 1
        assert state.extras()[9900001]["tags_source"] == "steamspy"


def test_steam_key_never_sent_to_steamspy(tmp_path):
    adapter = SampleSteam()
    with ExtractionState(tmp_path / "x.sqlite3") as state:
        build_extractor(state, adapter, key=ApiKey(FAKE_KEY), tags_source="steamspy").run([9900001])
    assert spy_calls(adapter) and all("key" not in q for q in spy_calls(adapter))


def test_extractor_requires_client_for_steamspy(tmp_path):
    from crawler.extract import Extractor

    with ExtractionState(tmp_path / "x.sqlite3") as state, pytest.raises(ValueError, match="SteamSpyClient"):
        Extractor(state, web=None, store=None, legacy_ids=frozenset(), tags_source="steamspy")


def test_cli_extract_with_steamspy(tmp_path, capsys):
    args = cli.build_parser().parse_args([
        "extract", "--appid", "9900001", "--tags-source", "steamspy", "--state", str(tmp_path / "x.sqlite3"),
        "--out", str(tmp_path / "out"), "--steamspy-cache", str(tmp_path / "spy.sqlite3"),
        "--reviews-state", str(tmp_path / "none.sqlite3")])
    assert cli.run_extract(args, {}, transport=make_transport(SampleSteam())) == 0
    out = capsys.readouterr().out
    assert "SteamSpy: 1 network request(s)" in out
    assert read(tmp_path / "out" / "games.csv")[0]["tags"].startswith("['Action', 'Indie'")


def test_cli_rejects_negative_ttl(tmp_path, capsys):
    args = cli.build_parser().parse_args(["extract", "--appid", "1", "--tags-source", "steamspy",
                                          "--steamspy-ttl-hours", "-1", "--state", str(tmp_path / "x")])
    assert cli.run_extract(args, {}) == cli.EXIT_USAGE

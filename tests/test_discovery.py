"""Discovery pipeline tests against an emulated Steam service (no network)."""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fakes import FAKE_KEY, reply
from requests.adapters import BaseAdapter

from crawler import cli, discovery, schema
from crawler import eligibility as e
from crawler.credentials import ApiKey, MissingCredentialError
from crawler.discovery import Discovery, DiscoveryConfigError, DiscoveryOptions
from crawler.discovery_state import DiscoveryState
from crawler.eligibility import DateWindow
from crawler.steam_api import SteamStoreAPI, SteamWebAPI
from crawler.transport import RetryPolicy, SteamTransport, SteamTransportError

TODAY = dt.date(2026, 10, 30)
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def game(appid, date="3 Mar, 2021", *, type="game", coming_soon=False, steam_appid=None,
         genres=("Action",), fullgame=None, name=None):
    data = {
        "type": type, "name": name or f"Game {appid}", "steam_appid": steam_appid or appid,
        "release_date": {"coming_soon": coming_soon, "date": date},
        "genres": [{"id": str(i), "description": g} for i, g in enumerate(genres)],
    }
    if fullgame:
        data["fullgame"] = {"appid": str(fullgame), "name": "Parent"}
    return data


class FakeSteamService(BaseAdapter):
    """Emulates GetAppList paging and per-app appdetails replies.

    ``details[appid]`` may be: a data dict (success), ``None`` (success=false),
    an int HTTP status, a ``(status, headers)`` tuple, an exception, or a list
    of those consumed one per request.
    """

    def __init__(self, apps=(), details=None):
        super().__init__()
        self.apps = {}  # appid -> (name, last_modified)
        for a in apps:
            self.add_app(*a) if isinstance(a, tuple) else self.add_app(a)
        self.details = dict(details or {})
        self.catalog_calls: list[dict[str, str]] = []
        self.details_calls: list[int] = []
        self.catalog_error: dict[int, BaseException] = {}  # page index -> exception

    def add_app(self, appid, last_modified=1_600_000_000, name=None):
        self.apps[appid] = (name or f"Game {appid}", last_modified)

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if url.path == "/IStoreService/GetAppList/v1/":
            return self._catalog(request, params)
        if url.path == "/api/appdetails":
            return self._details(request, int(params["appids"]))
        raise AssertionError(f"unexpected request {url.path}")

    def _respond(self, request, status, body, headers=None):
        status, content, hdrs = reply(status, body, headers=headers)
        resp = requests.Response()
        resp.status_code, resp._content, resp._content_consumed = status, content, True
        resp.headers = requests.structures.CaseInsensitiveDict(hdrs)
        resp.encoding, resp.url, resp.request = "utf-8", request.url, request
        return resp

    def _catalog(self, request, params):
        index = len(self.catalog_calls)
        self.catalog_calls.append(params)
        assert params["key"] == FAKE_KEY and params["include_games"] == "true" and params["include_dlc"] == "false"
        if index in self.catalog_error:
            raise self.catalog_error.pop(index)
        after = int(params.get("last_appid", 0))
        since = int(params.get("if_modified_since", 0))
        size = int(params["max_results"])
        matching = [a for a in sorted(self.apps) if a > after and self.apps[a][1] > since]
        page = matching[:size]
        response = {"apps": [{"appid": a, "name": self.apps[a][0], "last_modified": self.apps[a][1],
                              "price_change_number": 1} for a in page]}
        if len(matching) > size:
            response["have_more_results"] = True
        if page:
            response["last_appid"] = page[-1]
        return self._respond(request, 200, {"response": response})

    def _details(self, request, appid):
        self.details_calls.append(appid)
        item = self.details.get(appid, game(appid))
        if isinstance(item, list):
            item = item.pop(0) if len(item) > 1 else item[0]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, int):
            return self._respond(request, item, {})
        if isinstance(item, tuple):
            return self._respond(request, item[0], {}, headers=item[1])
        entry = {"success": False} if item is None else {"success": True, "data": item}
        return self._respond(request, 200, {str(appid): entry})

    def close(self):
        pass


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 10, 30, 12, 0, tzinfo=dt.timezone.utc)

    def now(self):
        self.t += dt.timedelta(seconds=1)
        return self.t

    def sleep(self, seconds):
        self.t += dt.timedelta(seconds=seconds)

    def mono(self):
        return self.t.timestamp()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def state(tmp_path):
    with DiscoveryState(tmp_path / "state.sqlite3") as s:
        yield s


def make_transport(service, clock):
    session = requests.Session()
    session.mount("https://", service)
    return SteamTransport(session=session, retry=RetryPolicy(max_attempts=2, backoff_base=1.0),
                          min_intervals={}, sleep=clock.sleep, clock=clock.mono, wall_clock=clock.mono,
                          rand=lambda: 0.0)


def make_discovery(service, state, clock, legacy=frozenset(), key=FAKE_KEY):
    transport = make_transport(service, clock)

    def web_factory():
        if key is None:
            raise MissingCredentialError("no key")
        return SteamWebAPI(transport, ApiKey(key))

    return Discovery(state, web_factory=web_factory, store=SteamStoreAPI(transport), legacy_ids=frozenset(legacy),
                     today=lambda: TODAY, now=clock.now, log=lambda _: None)


def run_and_export(d, tmp_path, **opts):
    report = d.run(DiscoveryOptions(**opts))
    discovery.export(d.state, tmp_path / "out", d.state.get_run(report.run_id), d.legacy_ids, report)
    return report


def read(tmp_path, name):
    with open(tmp_path / "out" / f"{name}.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def ids(rows):
    return [int(r["appid"]) for r in rows]


# --- pagination -----------------------------------------------------------------------

def test_all_catalog_pages_are_processed(state, clock, tmp_path):
    service = FakeSteamService(apps=range(1, 26))
    report = run_and_export(make_discovery(service, state, clock), tmp_path, page_size=10)
    assert [c.get("last_appid") for c in service.catalog_calls] == [None, "10", "20"]
    assert all(c["max_results"] == "10" for c in service.catalog_calls)
    assert ids(read(tmp_path, "discovered")) == list(range(1, 26))
    assert sorted(service.details_calls) == list(range(1, 26))
    assert report.finished and state.get_run(report.run_id).catalog_pages == 3
    assert state.get_run(report.run_id).catalog_complete


def test_exact_page_boundary(state, clock, tmp_path):
    service = FakeSteamService(apps=range(1, 21))
    run_and_export(make_discovery(service, state, clock), tmp_path, page_size=10)
    assert len(service.catalog_calls) == 2 and len(read(tmp_path, "discovered")) == 20


def test_empty_catalog_finishes(state, clock, tmp_path):
    report = run_and_export(make_discovery(FakeSteamService(), state, clock), tmp_path)
    assert report.finished and read(tmp_path, "discovered") == []


def test_catalog_failure_checkpoints_and_resume_continues_from_cursor(state, clock, tmp_path):
    service = FakeSteamService(apps=range(1, 26))
    service.catalog_error = {1: requests.ConnectionError("reset"), 2: requests.ConnectionError("reset")}
    d = make_discovery(service, state, clock)
    with pytest.raises(SteamTransportError):
        d.run(DiscoveryOptions(page_size=10))
    run = state.unfinished_run()
    assert run.catalog_cursor == 10 and run.catalog_pages == 1 and not run.catalog_complete
    assert service.details_calls == []

    service.catalog_calls.clear()
    report = run_and_export(make_discovery(service, state, clock), tmp_path, page_size=10, resume=True)
    assert [c.get("last_appid") for c in service.catalog_calls] == ["10", "20"]  # page 1 not refetched
    assert report.finished and ids(read(tmp_path, "discovered")) == list(range(1, 26))


# --- legacy ids and duplicates -----------------------------------------------------------

def test_legacy_appids_are_never_fetched_or_eligible(state, clock, tmp_path):
    legacy = {2, 4, 6}
    service = FakeSteamService(apps=range(1, 8), details={7: game(7, steam_appid=4)})  # 7 redirects to legacy 4
    run_and_export(make_discovery(service, state, clock, legacy), tmp_path)
    assert not legacy & set(service.details_calls)
    eligible, excluded = read(tmp_path, "eligible"), read(tmp_path, "excluded")
    assert ids(eligible) == [1, 3, 5]
    by_id = {int(r["appid"]): r for r in excluded}
    assert {a for a, r in by_id.items() if r["reason"] == e.IN_LEGACY_DATASET} == legacy
    assert by_id[7]["reason"] == e.STORE_APPID_MISMATCH and "steam_appid=4 (in legacy dataset)" in by_id[7]["detail"]


def test_each_appid_appears_in_exactly_one_output(state, clock, tmp_path):
    details = {3: 500, 4: game(4, type="dlc"), 5: game(5, "TBA"), 6: None}
    service = FakeSteamService(apps=range(1, 9), details=details)
    run_and_export(make_discovery(service, state, clock, legacy={8}), tmp_path, page_size=3)
    outputs = [ids(read(tmp_path, n)) for n in ("eligible", "excluded", "failed")]
    flat = [a for out in outputs for a in out]
    assert sorted(flat) == list(range(1, 9)) and len(flat) == len(set(flat))
    assert 8 not in outputs[0]
    discovered = ids(read(tmp_path, "discovered"))
    assert len(discovered) == len(set(discovered))


def test_repeated_full_runs_do_not_duplicate_or_refetch(state, clock, tmp_path):
    service = FakeSteamService(apps=range(1, 6))
    run_and_export(make_discovery(service, state, clock), tmp_path)
    service.details_calls.clear()
    run_and_export(make_discovery(service, state, clock), tmp_path)
    assert service.details_calls == []  # nothing changed, facts are reused
    assert ids(read(tmp_path, "discovered")) == [1, 2, 3, 4, 5]
    assert ids(read(tmp_path, "eligible")) == [1, 2, 3, 4, 5]


def test_load_legacy_appids_from_original_csv(legacy_csv):
    ids_ = discovery.load_legacy_appids(legacy_csv)
    assert len(ids_) == 23034 and 976730 in ids_


# --- reasons ------------------------------------------------------------------------------

def test_exclusion_reasons_are_recorded(state, clock, tmp_path):
    details = {
        1: game(1, "3 Mar, 2021"),
        2: game(2, type="dlc"),
        3: game(3, type="demo"),
        4: game(4, type="music"),
        5: game(5, type="application"),
        6: game(6, genres=("Utilities",)),
        7: game(7, "Q1 2027", coming_soon=True),
        8: game(8, "1 Jun, 2015"),
        9: None,
        10: game(10, fullgame=1),
        11: game(11, ""),
    }
    service = FakeSteamService(apps=range(1, 12), details=details)
    run_and_export(make_discovery(service, state, clock), tmp_path)
    reasons = {int(r["appid"]): r["reason"] for r in read(tmp_path, "excluded")}
    assert reasons == {
        2: e.DLC, 3: e.DEMO, 4: e.SOUNDTRACK, 5: e.SOFTWARE, 6: e.SOFTWARE, 7: e.UNRELEASED,
        8: e.RELEASED_BEFORE_WINDOW, 9: e.STORE_DETAILS_UNAVAILABLE, 10: e.LINKED_TO_PARENT_GAME,
        11: e.RELEASE_DATE_MISSING,
    }
    (row,) = read(tmp_path, "eligible")
    assert row["appid"] == "1" and row["reason"] == e.RELEASED_IN_WINDOW
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert summary["counts"] == {"discovered": 11, "eligible": 1, "excluded": 10, "failed": 0, "pending": 0}
    assert summary["window"] == {"start": "2020-01-01", "end": "2026-10-30"}


def test_ambiguous_release_dates_are_not_fabricated(state, clock, tmp_path):
    details = {
        1: game(1, "Early 2021"),
        2: game(2, "03/04/2021"),
        3: game(3, "Oct 2026"),         # straddles the default end (today)
        4: game(4, "2021"),             # whole year inside the window: eligible, but year precision only
        5: game(5, "Q2 2022"),
        6: game(6, "Coming soon"),
        7: game(7, "14 Feb, 2022"),
    }
    service = FakeSteamService(apps=range(1, 8), details=details)
    run_and_export(make_discovery(service, state, clock), tmp_path)

    excluded = {int(r["appid"]): r for r in read(tmp_path, "excluded")}
    for appid in (1, 2, 3, 6):
        row = excluded[appid]
        assert row["reason"] == e.RELEASE_DATE_AMBIGUOUS
        assert row["release_date"] == details[appid]["release_date"]["date"]  # raw text kept verbatim
    for appid in (1, 2, 6):
        assert not ISO_DATE.search(excluded[appid]["detail"])  # no invented date anywhere

    eligible = {int(r["appid"]): r for r in read(tmp_path, "eligible")}
    assert set(eligible) == {4, 5, 7}
    assert (eligible[4]["release_date"], eligible[4]["release_date_precision"],
            eligible[4]["release_date_earliest"], eligible[4]["release_date_latest"]) == ("2021", "year", "2021-01-01", "2021-12-31")
    assert eligible[5]["release_date_precision"] == "quarter"
    assert eligible[7]["release_date_earliest"] == eligible[7]["release_date_latest"] == "2022-02-14"
    for row in eligible.values():
        exact = row["release_date_precision"] == "day"
        assert (row["release_date_earliest"] == row["release_date_latest"]) is exact


def test_window_change_reclassifies_without_refetching(state, clock, tmp_path):
    service = FakeSteamService(apps=[1, 2], details={1: game(1, "3 Mar, 2021"), 2: game(2, "3 Mar, 2024")})
    run_and_export(make_discovery(service, state, clock), tmp_path)
    assert ids(read(tmp_path, "eligible")) == [1, 2]
    service.details_calls.clear()
    window = DateWindow(dt.date(2020, 1, 1), dt.date(2022, 12, 31))
    run_and_export(make_discovery(service, state, clock), tmp_path, window=window)
    assert service.details_calls == []
    assert ids(read(tmp_path, "eligible")) == [1]
    assert {int(r["appid"]): r["reason"] for r in read(tmp_path, "excluded")} == {2: e.RELEASED_AFTER_WINDOW}


# --- limit, checkpoint, resume ---------------------------------------------------------------

def test_limit_then_resume_without_refetching(state, clock, tmp_path):
    service = FakeSteamService(apps=range(1, 11))
    report = run_and_export(make_discovery(service, state, clock), tmp_path, limit=3)
    assert report.stop_reason == discovery.LIMIT_REACHED and not report.finished
    assert service.details_calls == [10, 9, 8] and report.pending == 7
    assert state.unfinished_run().run_id == report.run_id
    pending_rows = [r for r in read(tmp_path, "discovered") if r["status"] == "pending"]
    assert len(pending_rows) == 7

    report = run_and_export(make_discovery(service, state, clock), tmp_path, resume=True, limit=4)
    assert service.details_calls == [10, 9, 8, 7, 6, 5, 4]
    report = run_and_export(make_discovery(service, state, clock), tmp_path, resume=True)
    assert report.finished and sorted(service.details_calls) == list(range(1, 11))
    assert len(service.details_calls) == 10  # nothing fetched twice
    assert len(service.catalog_calls) == 1   # catalog was not re-crawled on resume


def test_limit_zero_fetches_nothing(state, clock, tmp_path):
    service = FakeSteamService(apps=[1, 2])
    report = run_and_export(make_discovery(service, state, clock), tmp_path, limit=0)
    assert service.details_calls == [] and report.stop_reason == discovery.LIMIT_REACHED and report.pending == 2


def test_interrupt_during_metadata_then_resume(state, clock, tmp_path):
    service = FakeSteamService(apps=range(1, 6), details={3: [KeyboardInterrupt(), game(3)]})
    with pytest.raises(KeyboardInterrupt):
        make_discovery(service, state, clock).run(DiscoveryOptions())
    assert service.details_calls == [5, 4, 3]
    report = run_and_export(make_discovery(service, state, clock), tmp_path, resume=True)
    assert service.details_calls == [5, 4, 3, 3, 2, 1] and report.finished
    assert ids(read(tmp_path, "eligible")) == [1, 2, 3, 4, 5]


def test_unfinished_run_requires_resume_or_restart(state, clock, tmp_path):
    service = FakeSteamService(apps=range(1, 4))
    first = make_discovery(service, state, clock).run(DiscoveryOptions(limit=1))
    with pytest.raises(DiscoveryConfigError, match="--resume"):
        make_discovery(service, state, clock).run(DiscoveryOptions())
    with pytest.raises(DiscoveryConfigError, match="cannot be resumed"):
        make_discovery(service, state, clock).run(
            DiscoveryOptions(resume=True, window=DateWindow(dt.date(2021, 1, 1), TODAY)))
    second = make_discovery(service, state, clock).run(DiscoveryOptions(restart=True))
    assert second.run_id != first.run_id and second.finished
    assert state.get_run(first.run_id).status == "abandoned"


def test_resume_without_unfinished_run_is_an_error(state, clock):
    with pytest.raises(DiscoveryConfigError, match="no unfinished run"):
        make_discovery(FakeSteamService(), state, clock).run(DiscoveryOptions(resume=True))


def test_missing_key_does_not_create_a_run(state, clock):
    with pytest.raises(MissingCredentialError):
        make_discovery(FakeSteamService(apps=[1]), state, clock, key=None).run(DiscoveryOptions())
    assert state.unfinished_run() is None


def test_resume_after_catalog_needs_no_key(state, clock, tmp_path):
    service = FakeSteamService(apps=range(1, 4))
    make_discovery(service, state, clock).run(DiscoveryOptions(limit=1))
    report = run_and_export(make_discovery(service, state, clock, key=None), tmp_path, resume=True)
    assert report.finished


# --- failures -------------------------------------------------------------------------------

def test_failed_lookups_are_saved_and_retried_next_run(state, clock, tmp_path):
    service = FakeSteamService(apps=[1, 2, 3], details={2: [500, 500, game(2)]})
    report = run_and_export(make_discovery(service, state, clock), tmp_path)
    assert report.finished and report.lookup_failures == 1
    (row,) = read(tmp_path, "failed")
    assert (row["appid"], row["reason"], row["error"], row["attempts"]) == ("2", e.FETCH_ERROR, "SteamHTTPError", "1")
    assert "HTTP 500" in row["detail"]
    assert service.details_calls.count(2) == 2  # transport retried once within the lookup

    report = run_and_export(make_discovery(service, state, clock), tmp_path)
    assert read(tmp_path, "failed") == [] and ids(read(tmp_path, "eligible")) == [1, 2, 3]


def test_rate_limit_stops_without_marking_failed(state, clock, tmp_path):
    # Two 429s exhaust the 2-attempt retry policy; the third reply succeeds on resume.
    service = FakeSteamService(apps=[1, 2, 3], details={2: [(429, {"Retry-After": "1"})] * 2 + [game(2)]})
    report = run_and_export(make_discovery(service, state, clock), tmp_path)
    assert report.stop_reason == discovery.RATE_LIMITED and report.pending == 2
    assert read(tmp_path, "failed") == []
    report = run_and_export(make_discovery(service, state, clock), tmp_path, resume=True)
    assert report.finished and ids(read(tmp_path, "eligible")) == [1, 2, 3]


def test_too_many_consecutive_failures_stops(state, clock, tmp_path):
    service = FakeSteamService(apps=range(1, 11), details={a: 503 for a in range(1, 11)})
    d = make_discovery(service, state, clock)
    report = d.run(DiscoveryOptions(max_consecutive_failures=3))
    assert report.stop_reason == discovery.TOO_MANY_FAILURES and report.lookup_failures == 3


# --- incremental ------------------------------------------------------------------------------

def test_incremental_uses_last_finished_catalog_time(state, clock, tmp_path):
    service = FakeSteamService(apps=[(1, 1_600_000_000), (2, 1_600_000_000)],
                               details={2: [game(2, "Q1 2027", coming_soon=True), game(2, "2 Feb, 2026")]})
    first = run_and_export(make_discovery(service, state, clock), tmp_path)
    started = state.get_run(first.run_id).catalog_started_at

    later = started + 3600
    service.add_app(2, later)   # app 2 modified (released)
    service.add_app(3, later)   # new app
    service.catalog_calls.clear()
    service.details_calls.clear()
    report = run_and_export(make_discovery(service, state, clock), tmp_path, incremental=True)
    assert report.mode == "incremental" and report.finished
    assert service.catalog_calls[0]["if_modified_since"] == str(started)
    assert sorted(service.details_calls) == [2, 3]   # app 1 unchanged: not refetched
    assert report.new_apps == 1
    assert ids(read(tmp_path, "eligible")) == [1, 2, 3]
    assert ids(read(tmp_path, "discovered")) == [1, 2, 3]


def test_incremental_requires_a_finished_run(state, clock):
    with pytest.raises(DiscoveryConfigError, match="previously finished run"):
        make_discovery(FakeSteamService(), state, clock).run(DiscoveryOptions(incremental=True))


def test_recheck_unreleased(state, clock, tmp_path):
    service = FakeSteamService(apps=[1, 2], details={2: [game(2, "Coming soon", coming_soon=True), game(2, "1 Oct, 2026")]})
    run_and_export(make_discovery(service, state, clock), tmp_path)
    service.details_calls.clear()
    run_and_export(make_discovery(service, state, clock), tmp_path)
    assert service.details_calls == []          # not rechecked by default
    run_and_export(make_discovery(service, state, clock), tmp_path, recheck_unreleased=True)
    assert service.details_calls == [2]         # only the unreleased app
    assert ids(read(tmp_path, "eligible")) == [1, 2]


# --- export format and CLI ------------------------------------------------------------------------

def test_export_files_have_fixed_headers(state, clock, tmp_path):
    run_and_export(make_discovery(FakeSteamService(apps=[1]), state, clock), tmp_path)
    for name, fields in [("discovered", discovery.DISCOVERED_FIELDS), ("eligible", discovery.ELIGIBLE_FIELDS),
                         ("excluded", discovery.EXCLUDED_FIELDS), ("failed", discovery.FAILED_FIELDS)]:
        with open(tmp_path / "out" / f"{name}.csv", newline="", encoding="utf-8") as f:
            assert tuple(next(csv.reader(f))) == fields


def _legacy_csv(path, appids):
    rows = [{
        "id": str(a), "title": f"Old {a}", "genres": "['Action']", "developer": "Dev", "publisher": "Pub",
        "release_date": "3 Dec, 2019", "app_name": f"Old {a}", "specs": "['Single-player']", "tags": "['Action']",
        "price": "10", "sentiment": "Positive", "early_access": "FALSE", "metascore": "",
    } for a in appids]
    schema.write_rows(path, rows)
    return path


def test_cli_discover_end_to_end(tmp_path, clock, capsys):
    legacy = _legacy_csv(tmp_path / "legacy.csv", [2])
    service = FakeSteamService(apps=[1, 2, 3], details={3: game(3, "1 Jun, 2019")})
    args = cli.build_parser().parse_args([
        "discover", "--state", str(tmp_path / "s.sqlite3"), "--out", str(tmp_path / "out"),
        "--legacy-csv", str(legacy), "--end", "2026-10-30", "--page-size", "2",
    ])
    code = cli.run_discover(args, {"STEAM_API_KEY": FAKE_KEY}, transport=make_transport(service, clock))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "eligible=1" in out and "excluded=2" in out and FAKE_KEY not in out
    assert ids(read(tmp_path, "eligible")) == [1]
    assert {r["reason"] for r in read(tmp_path, "excluded")} == {e.IN_LEGACY_DATASET, e.RELEASED_BEFORE_WINDOW}
    assert len(service.catalog_calls) == 2


def test_cli_discover_without_key_fails_cleanly(tmp_path, clock, capsys):
    legacy = _legacy_csv(tmp_path / "legacy.csv", [2])
    args = cli.build_parser().parse_args([
        "discover", "--state", str(tmp_path / "s.sqlite3"), "--out", str(tmp_path / "out"), "--legacy-csv", str(legacy),
    ])
    code = cli.run_discover(args, {}, transport=make_transport(FakeSteamService(apps=[1]), clock))
    assert code == cli.EXIT_ERROR and "STEAM_API_KEY is not set" in capsys.readouterr().out


def test_cli_rejects_inverted_window(tmp_path, clock, capsys):
    legacy = _legacy_csv(tmp_path / "legacy.csv", [2])
    args = cli.build_parser().parse_args(["discover", "--start", "2025-01-01", "--end", "2024-01-01",
                                          "--state", str(tmp_path / "s"), "--legacy-csv", str(legacy)])
    assert cli.run_discover(args, {}) == cli.EXIT_USAGE

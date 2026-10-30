"""Mocked failure scenarios through the whole pipeline: API errors, 429s, malformed data,
missing fields and interrupted runs. Every scenario ends with all quality checks passing,
and recovered runs reproduce the reference outputs byte for byte."""

from __future__ import annotations

import copy
import csv
import hashlib
import json

import pytest
from fakes import FAKE_KEY, make_transport
from sample_data import APPDETAILS, SampleSteam

from crawler import cli, pipeline, quality
from crawler.transport import RetryPolicy

CATALOG = {a: 1_700_000_000 for a in APPDETAILS}


def steam(**kwargs):
    return SampleSteam(catalog=CATALOG, **kwargs)


def run(base, legacy_csv, adapter, *extra, command="run", attempts=1):
    argv = [command, "--base-dir", str(base), "--work-dir", str(base / "work"), "--legacy-csv", str(legacy_csv)]
    if command in ("run", "discover"):
        argv += ["--end-date", "2026-10-30"]
    return cli.main([*argv, *extra], environ={"STEAM_API_KEY": FAKE_KEY},
                    transport=make_transport(adapter, retry=RetryPolicy(max_attempts=attempts, backoff_base=0.01)))


def checks_pass(base, legacy_csv):
    ws = pipeline.Workspace.at(base, base / "work", legacy_csv)
    failing = {c.name: c.failures for c in quality.check_outputs(ws) if not c.ok}
    assert failing == {}


def new_ids(base):
    with open(base / pipeline.NEW_CSV, newline="", encoding="utf-8") as f:
        return [int(r["id"]) for r in csv.DictReader(f)]


def failed_rows(base):
    with open(base / "work" / pipeline.FAILED_CSV, newline="", encoding="utf-8") as f:
        return [(int(r["appid"]), r["stage"], r["error"]) for r in csv.DictReader(f)]


def provenance(base, appid, field):
    with open(base / pipeline.PROVENANCE_CSV, newline="", encoding="utf-8") as f:
        return next(r for r in csv.DictReader(f) if r["appid"] == str(appid) and r["field"] == field)


def on(path, appid=None, *, param="appids"):
    """Match a request by path and (optionally) AppID."""
    def match(p, params):
        if p != path:
            return False
        if appid is None:
            return True
        if param == "input_json":
            return f'"appid":{appid}' in params.get("input_json", "")
        return params.get(param) == str(appid)
    return match


def inject(adapter, match, answer, *, times=None):
    """Answer matching requests with ``answer`` (a value or exception), optionally only ``times`` times."""
    state = {"left": times}

    def override(path, params):
        if not match(path, params) or state["left"] == 0:
            return None
        if state["left"] is not None:
            state["left"] -= 1
        if isinstance(answer, BaseException):
            raise answer
        return answer
    adapter.overrides.append(override)
    return override


APPDETAILS_PATH = "/api/appdetails"
ITEMS = "/IStoreBrowseService/GetItems/v1/"
REVIEWS = "/IUserReviewsService/GetAppReviews/v1/"
CATALOG_PATH = "/IStoreService/GetAppList/v1/"
TAGLIST = "/IStoreService/GetTagList/v1/"


@pytest.fixture(scope="module")
def reference(tmp_path_factory, legacy_csv):
    base = tmp_path_factory.mktemp("reference")
    assert run(base, legacy_csv, steam()) == 0
    return {n: (base / n).read_bytes() for n in (pipeline.NEW_CSV, pipeline.MERGED_CSV)}


def same_as_reference(base, reference):
    return {n: (base / n).read_bytes() for n in reference} == reference


# --- API failures -------------------------------------------------------------------------------

def test_server_error_is_recorded_then_retried(tmp_path, legacy_csv, reference):
    adapter = steam()
    inject(adapter, on(APPDETAILS_PATH, 9900002), (500, {}))
    assert run(tmp_path, legacy_csv, adapter) == 0
    assert 9900002 not in new_ids(tmp_path)
    assert failed_rows(tmp_path) == [(9900002, "discover", "SteamHTTPError")]
    checks_pass(tmp_path, legacy_csv)

    adapter.overrides.clear()
    assert run(tmp_path, legacy_csv, adapter) == 0  # next run retries the failed lookup
    assert failed_rows(tmp_path) == [] and same_as_reference(tmp_path, reference)


def test_network_errors_are_retried_within_a_request(tmp_path, legacy_csv, reference):
    import requests

    adapter = steam()
    inject(adapter, on(APPDETAILS_PATH, 9900001), requests.ConnectionError("reset"), times=1)
    inject(adapter, on(ITEMS, 9900002, param="input_json"), requests.Timeout("slow"), times=1)
    assert run(tmp_path, legacy_csv, adapter, attempts=2) == 0
    assert failed_rows(tmp_path) == [] and same_as_reference(tmp_path, reference)


# --- 429 ----------------------------------------------------------------------------------------------

def test_429_then_success_is_transparent(tmp_path, legacy_csv, reference):
    adapter = steam()
    inject(adapter, on(APPDETAILS_PATH, 9900003), (429, {}, {"Retry-After": "2"}), times=1)
    assert run(tmp_path, legacy_csv, adapter, attempts=2) == 0
    assert same_as_reference(tmp_path, reference)


@pytest.mark.parametrize(
    ("match", "stage"),
    [(on(APPDETAILS_PATH, 9900003), "discover"), (on(ITEMS, 9900001, param="input_json"), "crawl"),
     (on(REVIEWS, 9900002, param="appid"), "crawl"), (on(CATALOG_PATH), "discover")],
)
def test_persistent_429_stops_without_failing_then_resumes(tmp_path, legacy_csv, reference, match, stage):
    adapter = steam()
    inject(adapter, match, (429, {}, {"Retry-After": "1"}))
    assert run(tmp_path, legacy_csv, adapter) == cli.EXIT_TEMPFAIL
    if (tmp_path / "work" / pipeline.FAILED_CSV).exists():
        assert failed_rows(tmp_path) == []  # a throttled request is pending, never "failed"
    status = json.loads((tmp_path / "work" / "crawl_status.json").read_text())
    assert status["stages"][stage]["state"] == "incomplete"
    adapter.overrides.clear()
    assert run(tmp_path, legacy_csv, adapter, "--resume") == 0
    assert same_as_reference(tmp_path, reference)
    checks_pass(tmp_path, legacy_csv)


def test_retry_after_beyond_the_cap_is_not_waited(tmp_path, legacy_csv):
    adapter = steam()
    inject(adapter, on(APPDETAILS_PATH, 9900001), (429, {}, {"Retry-After": "86400"}))
    assert run(tmp_path, legacy_csv, adapter, attempts=3) == cli.EXIT_TEMPFAIL
    assert sum(1 for p, q in adapter.calls if p == APPDETAILS_PATH and q["appids"] == "9900001") == 1


# --- malformed data -------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "answer",
    [b"<html>Service Unavailable</html>", (200, None), (200, []), (200, {"9900001": {"success": "yes"}}),
     (200, {"9900001": {"success": True, "data": "nope"}}), (200, {"9999": {"success": True, "data": {}}})],
)
def test_malformed_appdetails_is_a_recorded_failure(tmp_path, legacy_csv, answer):
    adapter = steam()
    inject(adapter, on(APPDETAILS_PATH, 9900001), answer)
    assert run(tmp_path, legacy_csv, adapter) == 0
    assert 9900001 not in new_ids(tmp_path)
    assert [(a, s) for a, s, _ in failed_rows(tmp_path)] == [(9900001, "discover")]
    checks_pass(tmp_path, legacy_csv)


@pytest.mark.parametrize(
    ("answer", "ea_status"),
    [((200, {"response": {"store_items": "oops"}}), "source_unavailable"),
     # The item itself is readable (no is_early_access flag); only its tag list is malformed.
     ((200, {"response": {"store_items": [{"appid": 9900001, "success": 1, "tags": [{"tagid": "x"}]}]}}), "unverified"),
     (b"not json", "source_unavailable")],
)
def test_malformed_store_item_blanks_tags_and_early_access(tmp_path, legacy_csv, answer, ea_status):
    adapter = steam()
    inject(adapter, on(ITEMS, 9900001, param="input_json"), answer)
    assert run(tmp_path, legacy_csv, adapter) == 0
    assert 9900001 in new_ids(tmp_path)
    assert provenance(tmp_path, 9900001, "tags")["status"] == "source_unavailable"
    ea = provenance(tmp_path, 9900001, "early_access")
    assert ea["value"] == "" and ea["status"] == ea_status
    checks_pass(tmp_path, legacy_csv)


@pytest.mark.parametrize(
    "answer",
    [(200, {"response": {"weird": 1}}),
     (200, {"response": {"query_summary": {"review_score_desc": "Very Positive", "total_positive": "lots",
                                           "total_negative": 1, "total_reviews": 2}}}),
     (200, {"response": {"query_summary": {"review_score_desc": "Very Positive", "total_positive": 3,
                                           "total_negative": 1, "total_reviews": 4}}})],
)
def test_malformed_review_summary_never_yields_sentiment(tmp_path, legacy_csv, answer):
    adapter = steam()
    inject(adapter, on(REVIEWS, 9900001, param="appid"), answer)
    assert run(tmp_path, legacy_csv, adapter) == 0
    p = provenance(tmp_path, 9900001, "sentiment")
    assert p["value"] == "" and p["status"] == "unrecognized" and "invalid_response" in p["note"]
    checks_pass(tmp_path, legacy_csv)


def test_malformed_tag_list_blanks_all_steam_tags(tmp_path, legacy_csv):
    adapter = steam()
    inject(adapter, on(TAGLIST), (200, {"response": {"tags": "nope"}}))
    assert run(tmp_path, legacy_csv, adapter) == 0
    with open(tmp_path / pipeline.NEW_CSV, newline="", encoding="utf-8") as f:
        assert all(r["tags"] == "" for r in csv.DictReader(f))
    checks_pass(tmp_path, legacy_csv)


# --- missing fields -------------------------------------------------------------------------------------

def test_missing_optional_fields_become_explained_blanks(tmp_path, legacy_csv):
    details = copy.deepcopy(APPDETAILS)
    for k in ("genres", "categories", "developers", "publishers", "price_overview", "metacritic"):
        details[9900001].pop(k)
    adapter = steam(appdetails=details)
    assert run(tmp_path, legacy_csv, adapter) == 0
    for field, status in [("genres", "missing"), ("specs", "missing"), ("developer", "missing"),
                          ("publisher", "missing"), ("price", "missing"), ("metascore", "missing")]:
        p = provenance(tmp_path, 9900001, field)
        assert (p["value"], p["status"]) == ("", status)
    assert provenance(tmp_path, 9900001, "early_access")["value"] == "FALSE"  # explicit store flag still decides
    checks_pass(tmp_path, legacy_csv)


@pytest.mark.parametrize(
    ("drop", "reason"),
    [("release_date", "release_date_missing"), ("type", "app_type_missing")],
)
def test_missing_eligibility_fields_exclude_the_app(tmp_path, legacy_csv, drop, reason):
    details = copy.deepcopy(APPDETAILS)
    details[9900001].pop(drop)
    assert run(tmp_path, legacy_csv, steam(appdetails=details)) == 0
    assert 9900001 not in new_ids(tmp_path)
    with open(tmp_path / "work" / "discovery" / "excluded.csv", newline="", encoding="utf-8") as f:
        assert {r["appid"]: r["reason"] for r in csv.DictReader(f)}["9900001"] == reason
    checks_pass(tmp_path, legacy_csv)


def test_missing_name_is_a_metadata_failure(tmp_path, legacy_csv):
    details = copy.deepcopy(APPDETAILS)
    details[9900001]["name"] = ""
    assert run(tmp_path, legacy_csv, steam(appdetails=details)) == 0
    assert 9900001 not in new_ids(tmp_path)
    assert (9900001, "crawl:metadata", "normalization_error") in failed_rows(tmp_path)
    checks_pass(tmp_path, legacy_csv)


# --- interrupted runs --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "match",
    [on(CATALOG_PATH), on(APPDETAILS_PATH, 9900003), on(ITEMS, 9900002, param="input_json"),
     on(REVIEWS, 9900003, param="appid"), on(TAGLIST)],
    ids=["catalog", "discovery-appdetails", "crawl-store-item", "crawl-reviews", "crawl-tag-list"],
)
def test_interrupted_run_resumes_to_identical_outputs(tmp_path, legacy_csv, reference, match):
    adapter = steam()
    inject(adapter, match, KeyboardInterrupt(), times=1)
    assert run(tmp_path, legacy_csv, adapter) == cli.EXIT_INTERRUPTED
    status = json.loads((tmp_path / "work" / "crawl_status.json").read_text())
    assert "interrupted" in {s["state"] for s in status["stages"].values()}
    assert run(tmp_path, legacy_csv, adapter, "--resume") == 0
    assert same_as_reference(tmp_path, reference)
    assert not list(tmp_path.rglob(".*.tmp"))
    checks_pass(tmp_path, legacy_csv)


def test_crash_during_export_never_leaves_partial_outputs(tmp_path, legacy_csv, reference, monkeypatch):
    adapter = steam()
    assert run(tmp_path, legacy_csv, adapter, command="discover") == 0
    assert run(tmp_path, legacy_csv, adapter, command="export") == 0      # outputs exist, 0 games crawled yet
    old_merged = (tmp_path / pipeline.MERGED_CSV).read_bytes()
    assert old_merged == legacy_csv.read_bytes()
    assert run(tmp_path, legacy_csv, adapter, command="crawl") == 0

    real_replace = pipeline.os.replace

    def crash_on_merged(src, dst):
        if str(dst).endswith(pipeline.MERGED_CSV):
            raise KeyboardInterrupt()  # simulated crash right before the merged file is swapped in
        return real_replace(src, dst)

    monkeypatch.setattr(pipeline.os, "replace", crash_on_merged)
    assert run(tmp_path, legacy_csv, adapter, command="export") == cli.EXIT_INTERRUPTED
    monkeypatch.setattr(pipeline.os, "replace", real_replace)

    assert not list(tmp_path.rglob(".*.tmp"))                                # no temp file outlives the crash
    assert (tmp_path / pipeline.MERGED_CSV).read_bytes() == old_merged       # old complete version, not partial
    assert run(tmp_path, legacy_csv, adapter, command="validate") == cli.EXIT_ERROR  # mismatch is detected
    assert run(tmp_path, legacy_csv, adapter, command="export") == 0
    assert same_as_reference(tmp_path, reference)
    checks_pass(tmp_path, legacy_csv)


def test_original_csv_is_never_modified(legacy_csv, tmp_path):
    before = hashlib.sha256(legacy_csv.read_bytes()).hexdigest()
    adapter = steam()
    inject(adapter, on(APPDETAILS_PATH, 9900002), (500, {}))
    run(tmp_path, legacy_csv, adapter)
    run(tmp_path, legacy_csv, adapter, "--refresh")
    assert hashlib.sha256(legacy_csv.read_bytes()).hexdigest() == before == pipeline.LEGACY_SHA256

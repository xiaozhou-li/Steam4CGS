"""Review-summary collector: verification, request shape, failure handling, and sentiment provenance."""

from __future__ import annotations

import csv
import json
import random

import pytest
from fakes import FAKE_KEY
from sample_data import APPDETAILS, REVIEWS, SampleSteam, build_collector, build_extractor

from crawler import cli, extract, schema
from crawler import reviews as rv
from crawler.credentials import ApiKey
from crawler.extract import ExtractionState
from crawler.transport import SteamAuthError


def qs(desc, pos, neg, total=None, score=None):
    summary = {"total_positive": pos, "total_negative": neg, "total_reviews": pos + neg if total is None else total}
    if desc is not None:
        summary["review_score_desc"] = desc
    if score is not None:
        summary["review_score"] = score
    return {"query_summary": summary}


@pytest.fixture
def rstate(tmp_path):
    with rv.ReviewState(tmp_path / "reviews.sqlite3") as s:
        yield s


def read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def review_calls(adapter):
    return [q for p, q in adapter.calls if p == "/IUserReviewsService/GetAppReviews/v1/"]


# --- verification ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", schema.SENTIMENT_LABELS)
def test_score_labels_verified_with_ten_or_more_reviews(label):
    v = rv.verify_summary(qs(label, 7, 3, score=5))
    assert (v.status, v.sentiment, v.total_reviews, v.review_score) == (rv.OK, label, 10, 5)


@pytest.mark.parametrize("n", range(1, 10))
def test_few_reviews_label_must_match_count(n):
    assert rv.verify_summary(qs(f"{n} user reviews", n, 0)).sentiment == f"{n} user reviews"
    assert rv.verify_summary(qs(f"{n} user reviews", n + 1, 0)).status == rv.INVALID_RESPONSE


@pytest.mark.parametrize("desc", ["No user reviews", None, "  "])
def test_no_reviews(desc):
    v = rv.verify_summary(qs(desc, 0, 0))
    assert (v.status, v.sentiment, v.total_reviews) == (rv.NO_REVIEWS, "", 0)


def test_missing_label_keeps_counts():
    v = rv.verify_summary(qs(None, 40, 10))
    assert (v.status, v.sentiment, v.total_positive, v.total_negative, v.total_reviews) == (rv.MISSING_LABEL, "", 40, 10, 50)


def test_localized_label_is_unrecognized_not_translated():
    v = rv.verify_summary(qs("Très positives", 90, 10))
    assert (v.status, v.sentiment, v.review_score_desc) == (rv.UNRECOGNIZED_LABEL, "", "Très positives")


def test_flat_response_shape_is_accepted():
    v = rv.verify_summary({"review_score_desc": "Mixed", "total_positive": 6, "total_negative": 6, "total_reviews": 12})
    assert (v.status, v.sentiment) == (rv.OK, "Mixed")


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        ({}, "no review summary"),
        ([], "no review summary"),
        (None, "no review summary"),
        ({"reviews": [], "cursor": "*"}, "no review summary"),
        ({"query_summary": {"review_score_desc": "Very Positive"}}, "missing or invalid counts"),
        (qs("Mixed", 5, 5, total=11), "!= total_reviews"),
        (qs("Mixed", -1, 12, total=11), "missing or invalid counts"),
        (qs("Mixed", True, 11, total=12), "missing or invalid counts"),
        ({"query_summary": {"review_score_desc": "Mixed", "total_positive": "6", "total_negative": 6,
                            "total_reviews": 12}}, "missing or invalid counts"),
        (qs("Very Positive", 0, 0), "with total_reviews = 0"),
        (qs("No user reviews", 3, 0), "with total_reviews = 3"),
        (qs("Positive", 8, 1), "needs >= 10 reviews"),
        (qs("3 user reviews", 30, 0), "does not match"),
        (qs("12 user reviews", 12, 0), "not in the legacy vocabulary"),
    ],
)
def test_unverifiable_responses_never_produce_sentiment(response, reason):
    v = rv.verify_summary(response)
    assert v.sentiment == "" and v.status in (rv.INVALID_RESPONSE, rv.UNRECOGNIZED_LABEL)
    assert reason in v.detail


def test_fuzzed_responses_only_yield_verified_sentiment():
    """Property: a non-empty sentiment implies every verification rule held for that exact response."""
    rng = random.Random(20261030)
    labels = [*schema.SENTIMENT_LABELS, *(f"{n} user reviews" for n in range(0, 15)), "No user reviews", "",
              None, "Very positive", "Mostly Postive", 7]
    seen_ok = 0
    for _ in range(5000):
        pos, neg = rng.choice([0, 1, 2, 5, 9, 10, 50, 1000]), rng.choice([0, 1, 3, 9, 40])
        total = rng.choice([pos + neg, pos + neg, pos + neg + 1, max(0, pos + neg - 1)])
        summary = {"total_positive": pos, "total_negative": neg, "total_reviews": total,
                   "review_score_desc": rng.choice(labels)}
        for k in rng.sample(list(summary), rng.choice([0, 0, 0, 1])):
            summary.pop(k)
        v = rv.verify_summary(rng.choice([{"query_summary": summary}, summary]))
        if v.sentiment:
            seen_ok += 1
            assert v.status == rv.OK and v.sentiment == summary["review_score_desc"]
            assert summary["total_positive"] + summary["total_negative"] == summary["total_reviews"]
            schema.decode_sentiment(v.sentiment)  # inside the legacy vocabulary
            if v.sentiment in schema.SENTIMENT_LABELS:
                assert summary["total_reviews"] >= rv.LABEL_MIN_REVIEWS
            else:
                assert v.sentiment == f"{summary['total_reviews']} user reviews"
        else:
            assert v.status != rv.OK
    assert seen_ok > 100  # the generator does hit the verified path


# --- collector: requests ------------------------------------------------------------------------

def test_capability_check_runs_first_and_only_declared_params_are_sent(rstate):
    adapter = SampleSteam(review_params=["key", "appid", "language"])
    build_collector(rstate, adapter).run([9900001])
    assert [p for p, _ in adapter.calls] == ["/ISteamWebAPIUtil/GetSupportedAPIList/v1/",
                                             "/IUserReviewsService/GetAppReviews/v1/"]
    assert review_calls(adapter) == [{"appid": "9900001", "language": "all"}]
    record = rstate.records()[9900001]
    assert record.params_sent == "appid=9900001;language=all"
    assert record.params_not_supported == "num_per_page;purchase_type"
    assert (record.review_language, record.purchase_type) == ("all", "all")


def test_summary_only_one_request_per_game_and_no_pagination(rstate):
    adapter = SampleSteam(reviews={a: {**qs("Mixed", 6, 6), "cursor": "AoJ4", "reviews": [{"review": "text"}] * 3}
                                   for a in (1, 2, 3)})
    build_collector(rstate, adapter).run([1, 2, 3])
    calls = review_calls(adapter)
    assert [c["appid"] for c in calls] == ["1", "2", "3"]
    assert all(c["num_per_page"] == "0" and "cursor" not in c for c in calls)
    stored = json.dumps([r.to_json() for r in rstate.records().values()])
    assert "text" not in stored  # individual reviews are never kept


def test_endpoint_not_listed_stops_before_any_review_request(rstate):
    adapter = SampleSteam(review_params=None)
    with pytest.raises(rv.ReviewEndpointUnavailable, match="no STEAM_API_KEY"):
        build_collector(rstate, adapter).run([9900001])
    assert review_calls(adapter) == [] and rstate.records() == {}


def test_key_sent_only_when_configured_and_never_recorded(rstate):
    adapter = SampleSteam()
    build_collector(rstate, adapter, key=ApiKey(FAKE_KEY)).run([9900001])
    assert review_calls(adapter)[0]["key"] == FAKE_KEY
    assert FAKE_KEY not in rstate.records()[9900001].to_json()


def test_nothing_to_do_makes_no_requests(rstate):
    adapter = SampleSteam()
    build_collector(rstate, adapter).run([9900001])
    adapter.calls.clear()
    build_collector(rstate, adapter).run([9900001])
    assert adapter.calls == []


# --- collector: outcomes, failures, rate limits -------------------------------------------------------

def test_outcomes_for_each_case(rstate):
    report = build_collector(rstate, SampleSteam()).run(sorted(REVIEWS))
    records = rstate.records()
    assert {a: (r.status, r.sentiment) for a, r in records.items()} == {
        9900001: (rv.OK, "Very Positive"),
        9900002: (rv.OK, "3 user reviews"),
        9900003: (rv.NO_REVIEWS, ""),
        9900004: (rv.INVALID_RESPONSE, ""),
        9900005: (rv.API_ERROR, ""),
    }
    assert "HTTP 503" in records[9900005].detail
    assert report.statuses[rv.OK] == 2 and report.pending == 0


def test_api_errors_are_retried_next_run(rstate):
    adapter = SampleSteam(reviews={7: 503})
    build_collector(rstate, adapter).run([7])
    assert rstate.records()[7].status == rv.API_ERROR
    adapter.reviews[7] = qs("Positive", 20, 2)
    build_collector(rstate, adapter).run([7])
    assert (rstate.records()[7].status, rstate.records()[7].sentiment) == (rv.OK, "Positive")


def test_rate_limit_stops_and_leaves_game_pending(rstate):
    adapter = SampleSteam(reviews={1: qs("Mixed", 6, 6), 2: 429, 3: qs("Positive", 20, 2)})
    report = build_collector(rstate, adapter).run([1, 2, 3])
    assert report.stop_reason == "rate_limited" and report.pending == 2
    assert set(rstate.records()) == {1}  # the throttled game is not recorded as failed
    adapter.reviews[2] = qs("Negative", 1, 20)
    report = build_collector(rstate, adapter).run([1, 2, 3])
    assert report.stop_reason == "complete" and [c["appid"] for c in review_calls(adapter)][-2:] == ["2", "3"]


def test_auth_error_aborts(rstate):
    adapter = SampleSteam(reviews={1: 403})
    with pytest.raises(SteamAuthError):
        build_collector(rstate, adapter, key=ApiKey(FAKE_KEY)).run([1])


def test_limit_and_legacy_skip(rstate):
    adapter = SampleSteam(reviews={a: qs("Mixed", 6, 6) for a in range(1, 6)})
    report = build_collector(rstate, adapter, legacy_ids={2}).run(range(1, 6), limit=2)
    assert report.skipped_legacy == [2] and report.stop_reason == "limit_reached"
    assert [c["appid"] for c in review_calls(adapter)] == ["1", "3"] and report.pending == 2


def test_changed_settings_trigger_recollection(rstate):
    adapter = SampleSteam(reviews={1: qs("Mixed", 6, 6)})
    build_collector(rstate, adapter).run([1])
    build_collector(rstate, adapter, settings=rv.ReviewSettings(language="english", purchase_type="steam")).run([1])
    calls = review_calls(adapter)
    assert len(calls) == 2 and (calls[1]["language"], calls[1]["purchase_type"]) == ("english", "steam")
    assert (rstate.records()[1].review_language, rstate.records()[1].purchase_type) == ("english", "steam")


@pytest.mark.parametrize("kwargs", [{"purchase_type": "gift"}, {"language": ""}, {"language": "en&key=1"}])
def test_invalid_settings(kwargs):
    with pytest.raises(ValueError):
        rv.ReviewSettings(**kwargs)


# --- export -------------------------------------------------------------------------------------------

def test_review_summaries_csv_and_meta(rstate, tmp_path):
    build_collector(rstate, SampleSteam()).run(sorted(REVIEWS))
    rv.export(rstate, tmp_path, rv.ReviewSettings())
    rows = {int(r["appid"]): r for r in read(tmp_path / "review_summaries.csv")}
    assert tuple(next(iter(rows.values()))) == rv.SUMMARY_FIELDS
    assert {k: rows[9900001][k] for k in ("status", "sentiment", "review_score_desc", "review_score",
                                           "total_positive", "total_negative", "total_reviews", "review_language",
                                           "purchase_type", "retrieved_at")} == {
        "status": "ok", "sentiment": "Very Positive", "review_score_desc": "Very Positive", "review_score": "8",
        "total_positive": "1100", "total_negative": "134", "total_reviews": "1234", "review_language": "all",
        "purchase_type": "all", "retrieved_at": "2026-10-30T12:00:00Z"}
    assert rows[9900001]["source_url"] == (
        "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
        "?appid=9900001&language=all&purchase_type=all&num_per_page=0")
    assert rows[9900005]["total_reviews"] == "" and rows[9900005]["status"] == "api_error"
    meta = json.loads((tmp_path / "review_summaries.meta.json").read_text())
    assert meta["endpoint"] == "IUserReviewsService/GetAppReviews/v1"
    assert meta["settings"] == {"language": "all", "purchase_type": "all"}
    assert meta["declared_parameters"] == sorted(["key", "appid", "language", "purchase_type", "num_per_page"])


# --- sentiment provenance: end to end ---------------------------------------------------------------------

def test_every_games_sentiment_is_backed_by_a_verified_review_response(rstate, tmp_path):
    collector_steam, extractor_steam = SampleSteam(), SampleSteam()
    build_collector(rstate, collector_steam).run(sorted(APPDETAILS))
    with ExtractionState(tmp_path / "x.sqlite3") as xstate:
        build_extractor(xstate, extractor_steam).run(sorted(APPDETAILS))
        extract.export(xstate, tmp_path / "out", rstate.records())
    rv.export(rstate, tmp_path / "out", rv.ReviewSettings())

    games = read(tmp_path / "out" / "games.csv")
    summaries = {r["appid"]: r for r in read(tmp_path / "out" / "review_summaries.csv")}
    provenance = {(r["appid"], r["field"]): r for r in read(tmp_path / "out" / "provenance.csv")}
    served = {str(a): r for a, r in REVIEWS.items()}
    assert any(g["sentiment"] for g in games) and any(not g["sentiment"] for g in games)
    for g in games:
        s, p = summaries[g["id"]], provenance[(g["id"], "sentiment")]
        if g["sentiment"]:
            assert s["status"] == rv.OK and s["sentiment"] == s["review_score_desc"] == g["sentiment"]
            # ...and it is literally what the GetAppReviews response contained.
            assert served[g["id"]]["query_summary"]["review_score_desc"] == g["sentiment"]
            assert p["source_url"] == s["source_url"] and p["retrieved_at"] == s["retrieved_at"]
            assert p["source"] == "user_reviews:GetAppReviews.review_score_desc"
        else:
            assert s["status"] != rv.OK
    # The extractor never requests reviews; the collector made exactly one request per game.
    assert review_calls(extractor_steam) == []
    assert sorted(int(c["appid"]) for c in review_calls(collector_steam)) == sorted(APPDETAILS)


def test_without_review_records_games_have_no_sentiment(tmp_path):
    with ExtractionState(tmp_path / "x.sqlite3") as xstate:
        build_extractor(xstate, SampleSteam()).run(sorted(APPDETAILS))
        extract.export(xstate, tmp_path / "out")
    assert all(g["sentiment"] == "" for g in read(tmp_path / "out" / "games.csv"))
    statuses = {r["status"] for r in read(tmp_path / "out" / "provenance.csv") if r["field"] == "sentiment"}
    assert statuses == {"not_collected"}


# --- CLI ---------------------------------------------------------------------------------------------------

def _args(tmp_path, *extra):
    return cli.build_parser().parse_args(["reviews", "--state", str(tmp_path / "r.sqlite3"),
                                          "--out", str(tmp_path / "out"), *extra])


def test_cli_reviews(tmp_path, capsys):
    from fakes import make_transport

    code = cli.run_reviews(_args(tmp_path, "--appid", "9900001", "--appid", "9900003", "--purchase-type", "steam"),
                           {}, transport=make_transport(SampleSteam()))
    out = capsys.readouterr().out
    assert code == 0 and "review_summaries=2" in out and "ok=1" in out and "no_reviews=1" in out
    rows = read(tmp_path / "out" / "review_summaries.csv")
    assert {r["purchase_type"] for r in rows} == {"steam"}


def test_cli_reviews_endpoint_unavailable(tmp_path, capsys):
    from fakes import make_transport

    code = cli.run_reviews(_args(tmp_path, "--appid", "1"), {}, transport=make_transport(SampleSteam(review_params=None)))
    assert code == cli.EXIT_ERROR and "not available to this key" in capsys.readouterr().out

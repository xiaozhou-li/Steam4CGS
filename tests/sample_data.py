"""Synthetic Steam responses and the generator for docs/samples/.

Every app here is fictional (AppIDs 9900001-9900005, names start with
"Sample"); the payload shapes follow the Store/Web API responses the
extractor parses. Regenerate the committed samples with:

    python3.13 tests/sample_data.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = ROOT / "docs" / "samples"
FIXED_NOW = dt.datetime(2026, 10, 30, 12, 0, 0, tzinfo=dt.timezone.utc)

TAG_NAMES = {492: "Indie", 19: "Action", 21: "Adventure", 1774: "Shooter", 493: "Early Access",
             1662: "Survival", 1664: "Puzzle", 699: "Racing", 7208: "Point & Click", 4182: "Singleplayer"}


def _genres(*names):
    ids = {"Action": "1", "Adventure": "25", "Indie": "23", "Simulation": "28", "Racing": "9",
           "Early Access": "70", "Free to Play": "37", "Casual": "4"}
    return [{"id": ids[n], "description": n} for n in names]


def _categories(*pairs):
    return [{"id": i, "description": d} for i, d in pairs]


APPDETAILS = {
    # Full record, on sale, US-style date, categories in Steam's id order, one non-legacy category.
    9900001: {
        "type": "game", "name": "Sample: Kestrel & Crow", "steam_appid": 9900001, "is_free": False,
        "developers": ["Sample Studio", "Port House (Mac)"], "publishers": ["Example Publishing"],
        "genres": _genres("Indie", "Action", "Adventure"),
        "categories": _categories((1, "Multi-player"), (2, "Single-player"), (22, "Steam Achievements"),
                                  (23, "Steam Cloud"), (28, "Full controller support"), (62, "Family Sharing")),
        "price_overview": {"currency": "USD", "initial": 1999, "final": 1499, "discount_percent": 25,
                           "initial_formatted": "$19.99", "final_formatted": "$14.99"},
        "release_date": {"coming_soon": False, "date": "Mar 14, 2023"},
        "metacritic": {"score": 84, "url": "https://www.metacritic.com/game/sample-kestrel-crow"},
        "achievements": {"total": 40},
    },
    # Free Early Access game, few reviews, no Metacritic.
    9900002: {
        "type": "game", "name": "Sample Sandbox", "steam_appid": 9900002, "is_free": True,
        "developers": ["Sample Sandbox Team"], "publishers": ["Sample Sandbox Team"],
        "genres": _genres("Simulation", "Early Access", "Free to Play", "Indie"),
        "categories": _categories((2, "Single-player"), (9, "Co-op"), (38, "Online Co-op")),
        "release_date": {"coming_soon": False, "date": "2 Feb, 2024"},
    },
    # Sparse record: no genres, categories, developers or price; quarter-precision date.
    9900003: {
        "type": "game", "name": "Sample Puzzle <Deluxe>", "steam_appid": 9900003, "is_free": False,
        "developers": [], "publishers": [""],
        "release_date": {"coming_soon": False, "date": "Q3 2022"},
    },
    # Regular price, tags request fails.
    9900004: {
        "type": "game", "name": "Sample Racer", "steam_appid": 9900004, "is_free": False,
        "developers": ["Sample Motorworks"], "publishers": ["Sample Motorworks"],
        "genres": _genres("Racing", "Casual"),
        "categories": _categories((2, "Single-player"), (22, "Steam Achievements")),
        "price_overview": {"currency": "USD", "initial": 999, "final": 999, "discount_percent": 0},
        "release_date": {"coming_soon": False, "date": "10 Dec, 2021"},
    },
    9900005: None,  # success=false
}

# GetAppReviews responses (the "response" object), one per review outcome.
REVIEWS = {
    9900001: {"query_summary": {"review_score": 8, "review_score_desc": "Very Positive", "total_positive": 1100,
                                "total_negative": 134, "total_reviews": 1234}},
    9900002: {"query_summary": {"review_score": 0, "review_score_desc": "3 user reviews", "total_positive": 3,
                                "total_negative": 0, "total_reviews": 3}},
    9900003: {"query_summary": {"review_score": 0, "review_score_desc": "No user reviews", "total_positive": 0,
                                "total_negative": 0, "total_reviews": 0}},
    # Inconsistent: a score label with fewer than 10 reviews -> invalid_response, sentiment blank.
    9900004: {"query_summary": {"review_score": 6, "review_score_desc": "Positive", "total_positive": 8,
                                "total_negative": 1, "total_reviews": 9}},
    9900005: 503,  # API failure
}

REVIEW_METHOD_PARAMS = ["key", "appid", "language", "purchase_type", "num_per_page"]

STORE_ITEM_TAGS = {
    9900001: [{"tagid": 492, "weight": 300}, {"tagid": 19, "weight": 250}, {"tagid": 999999, "weight": 200},
              {"tagid": 7208, "weight": 150}, {"tagid": 21, "weight": 150}],
    9900002: [{"tagid": 493, "weight": 90}, {"tagid": 1662, "weight": 80}, {"tagid": 492, "weight": 10}],
    9900003: [],
    9900004: 500,  # HTTP 500 -> tags source unavailable
}


# GetItems is_early_access per app (missing key -> the field is absent from the item).
STORE_ITEM_EARLY_ACCESS = {9900001: False, 9900002: True}

# SteamSpy appdetails bodies (the optional tags source).
_SPY_TAGS_1 = {"Indie": 120, "Action": 120, "Point & Click": 80, "adventure": 40, "Adventure": 40,
               **{f"Sample Tag {i:02d}": 30 - i for i in range(18)}}  # 23 tags, with ties
STEAMSPY = {
    9900001: {"appid": 9900001, "name": "Sample: Kestrel & Crow", "positive": 1100, "negative": 134,
              "owners": "20,000 .. 50,000", "tags": _SPY_TAGS_1},
    9900002: {"appid": 9900002, "name": "Sample Sandbox", "tags": []},     # record without tags
    9900003: {"appid": 9900003, "name": None, "tags": []},                 # SteamSpy has no record
    9900004: 500,                                                          # SteamSpy unavailable
}


def _ach(name, pct):
    return {"name": name, "percent": pct}


# GetSchemaForGame bodies (only served when a key is sent) and global percentages.
SCHEMAS = {
    9900001: {"game": {"gameName": "Sample: Kestrel & Crow", "gameVersion": "3", "availableGameStats": {
        "achievements": [{"name": f"ACH_{i}", "defaultvalue": 0, "displayName": f"Feat {i}", "hidden": int(i == 3),
                          "description": "", "icon": "", "icongray": ""} for i in range(1, 5)]}}},
    9900003: {"game": {"gameName": "Sample Puzzle <Deluxe>", "gameVersion": "1",
                       "availableGameStats": {"stats": [{"name": "plays", "defaultvalue": 0}]}}},  # explicit zero
}
PERCENTAGES = {
    9900001: {"achievementpercentages": {"achievements": [
        _ach("ACH_1", 71.2), _ach("ACH_2", "9.5"), _ach("ACH_3", 0.8), _ach("ACH_4", 0.1)]}},
    9900003: {"achievementpercentages": {"achievements": []}},  # explicit: no achievements
    9900004: 500,                                                  # endpoint failure -> unknown
    # 9900002: no entry -> HTTP 403 (not available)
}


PLAYERS = {9900001: 1234, 9900002: 0, 9900004: 87}  # 9900003: Steam's {"result": 42} (no data)


class SampleSteam(BaseAdapter):
    """Serves the synthetic payloads above for the four endpoints the extractor uses."""

    def __init__(self, appdetails=None, reviews=None, tags=None, tag_names=None,
                 review_params=REVIEW_METHOD_PARAMS, early_access=None, steamspy=None, catalog=None,
                 schemas=None, percentages=None, players=None):
        super().__init__()
        # GetNumberOfCurrentPlayers: appid -> player count (int), (status,) tuple, body dict or exception.
        self.players = dict(PLAYERS if players is None else players)
        # ISteamUserStats fixtures: appid -> body (dict), HTTP status (int) or exception.
        self.schemas = dict(SCHEMAS if schemas is None else schemas)
        self.percentages = dict(PERCENTAGES if percentages is None else percentages)
        # GetAppList catalog: appid -> last_modified (default: every sample app).
        self.catalog = dict(catalog) if catalog is not None else {a: 1_700_000_000 for a in APPDETAILS}
        # Failure injection: callables (path, params) -> Response | (status, body) | bytes | None.
        # The first one returning non-None answers the request; it may also raise.
        self.overrides: list = []
        self.early_access = dict(STORE_ITEM_EARLY_ACCESS if early_access is None else early_access)
        self.steamspy = dict(STEAMSPY if steamspy is None else steamspy)
        self.review_params = review_params  # None: GetAppReviews not listed
        self.appdetails = dict(APPDETAILS if appdetails is None else appdetails)
        self.reviews = dict(REVIEWS if reviews is None else reviews)
        self.tags = dict(STORE_ITEM_TAGS if tags is None else tags)
        self.tag_names = TAG_NAMES if tag_names is None else tag_names
        self.calls: list[tuple[str, dict[str, str]]] = []

    def _respond(self, request, status, body):
        resp = requests.Response()
        resp.status_code = status
        resp._content = json.dumps(body).encode()
        resp._content_consumed = True
        resp.headers = CaseInsensitiveDict({"Content-Type": "application/json"})
        resp.encoding, resp.url, resp.request = "utf-8", request.url, request
        return resp

    def _respond_raw(self, request, status, content: bytes, content_type="text/html"):
        resp = requests.Response()
        resp.status_code = status
        resp._content = content
        resp._content_consumed = True
        resp.headers = CaseInsensitiveDict({"Content-Type": content_type})
        resp.encoding, resp.url, resp.request = "utf-8", request.url, request
        return resp

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}
        self.calls.append((url.path, params))
        for override in self.overrides:
            answer = override(url.path, params)
            if answer is None:
                continue
            if isinstance(answer, bytes):
                return self._respond_raw(request, 200, answer)
            if isinstance(answer, tuple):
                status, body, *headers = answer
                resp = self._respond(request, status, body)
                if headers:
                    resp.headers.update(headers[0])
                return resp
            return answer
        if url.path == "/IStoreService/GetAppList/v1/":
            after, size = int(params.get("last_appid", 0)), int(params["max_results"])
            since = int(params.get("if_modified_since", 0))
            matching = [a for a in sorted(self.catalog) if a > after and self.catalog[a] > since]
            page = matching[:size]
            response = {"apps": [{"appid": a, "name": f"Catalog {a}", "last_modified": self.catalog[a],
                                  "price_change_number": 1} for a in page]}
            if len(matching) > size:
                response["have_more_results"] = True
            if page:
                response["last_appid"] = page[-1]
            return self._respond(request, 200, {"response": response})
        if url.path == "/api/appdetails":
            appid = int(params["appids"])
            data = self.appdetails.get(appid)
            if isinstance(data, int):
                return self._respond(request, data, {})
            if isinstance(data, BaseException):
                raise data
            entry = {"success": True, "data": data} if data is not None else {"success": False}
            return self._respond(request, 200, {str(appid): entry})
        if url.path == "/ISteamWebAPIUtil/GetSupportedAPIList/v1/":
            interfaces = [{"name": "IStoreService", "methods": [
                {"name": "GetTagList", "version": 1, "httpmethod": "GET", "parameters": [{"name": "language"}]}]}]
            if self.review_params is not None:
                interfaces.append({"name": "IUserReviewsService", "methods": [
                    {"name": "GetAppReviews", "version": 1, "httpmethod": "GET",
                     "parameters": [{"name": n} for n in self.review_params]}]})
            return self._respond(request, 200, {"apilist": {"interfaces": interfaces}})
        if url.path == "/IUserReviewsService/GetAppReviews/v1/":
            summary = self.reviews.get(int(params["appid"]), {})
            if isinstance(summary, int):
                return self._respond(request, summary, {})
            return self._respond(request, 200, {"response": summary})
        if url.path == "/IStoreBrowseService/GetItems/v1/":
            appid = json.loads(params["input_json"])["ids"][0]["appid"]
            tags = self.tags.get(appid, [])
            if isinstance(tags, BaseException):
                raise tags
            if isinstance(tags, int):
                return self._respond(request, tags, {})
            item = {"item_type": 0, "id": appid, "success": 1, "visible": True, "appid": appid,
                    "tagids": [t["tagid"] for t in tags], "tags": tags}
            if appid in self.early_access:
                item["is_early_access"] = self.early_access[appid]
            return self._respond(request, 200, {"response": {"store_items": [item]}})
        if url.hostname == "steamspy.com" and url.path == "/api.php":
            assert params["request"] == "appdetails" and "key" not in params
            body = self.steamspy.get(int(params["appid"]), {"appid": int(params["appid"]), "name": None, "tags": []})
            if isinstance(body, int):
                return self._respond(request, body, {})
            return self._respond(request, 200, body)
        if url.path == "/ISteamUserStats/GetNumberOfCurrentPlayers/v1/":
            answer = self.players.get(int(params["appid"]), {"result": 42})  # Steam's "no data" answer
            if isinstance(answer, BaseException):
                raise answer
            if isinstance(answer, tuple):
                return self._respond(request, answer[0], {})
            if isinstance(answer, int):
                answer = {"player_count": answer, "result": 1}
            return self._respond(request, 200, {"response": answer})
        if url.path in ("/ISteamUserStats/GetSchemaForGame/v2/",
                        "/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/"):
            schema_call = "GetSchemaForGame" in url.path
            if schema_call and "key" not in params:
                return self._respond(request, 403, {})
            appid = int(params["appid" if schema_call else "gameid"])
            table = self.schemas if schema_call else self.percentages
            answer = table.get(appid, 403)
            if isinstance(answer, BaseException):
                raise answer
            if isinstance(answer, int):
                return self._respond(request, answer, {})
            return self._respond(request, 200, answer)
        if url.path == "/IStoreService/GetTagList/v1/":
            if isinstance(self.tag_names, int):
                return self._respond(request, self.tag_names, {})
            return self._respond(request, 200, {"response": {"version_hash": "sample",
                                 "tags": [{"tagid": k, "name": v} for k, v in self.tag_names.items()]}})
        raise AssertionError(f"unexpected request {url.path}")

    def close(self):
        pass


def build_extractor(state, adapter: SampleSteam, legacy_ids=frozenset(), key=None, *, tags_source="steam",
                    steamspy_cache=None, now=lambda: FIXED_NOW):
    from fakes import make_transport

    from crawler.extract import Extractor
    from crawler.steam_api import SteamStoreAPI, SteamWebAPI
    from crawler.steamspy import SteamSpyClient
    from crawler.transport import RetryPolicy

    transport = make_transport(adapter, retry=RetryPolicy(max_attempts=1))
    steamspy = SteamSpyClient(transport, steamspy_cache, now=now) if tags_source == "steamspy" else None
    return Extractor(state, web=SteamWebAPI(transport, key), store=SteamStoreAPI(transport, now=now),
                     legacy_ids=frozenset(legacy_ids), tags_source=tags_source, steamspy=steamspy,
                     now=now, log=lambda _: None)


def build_collector(state, adapter: SampleSteam, legacy_ids=frozenset(), key=None, settings=None):
    from fakes import make_transport

    from crawler import reviews as rv
    from crawler.steam_api import SteamWebAPI
    from crawler.transport import RetryPolicy

    transport = make_transport(adapter, retry=RetryPolicy(max_attempts=1))
    return rv.ReviewCollector(state, SteamWebAPI(transport, key), settings=settings or rv.ReviewSettings(),
                              legacy_ids=frozenset(legacy_ids), now=lambda: FIXED_NOW, log=lambda _: None)


def write_samples(out_dir: Path, state_dir: Path) -> dict[str, int]:
    from crawler import reviews as rv
    from crawler.extract import ExtractionState, export

    adapter = SampleSteam()
    with rv.ReviewState(state_dir / "reviews.sqlite3") as rstate:
        build_collector(rstate, adapter).run(sorted(APPDETAILS))
        rv.export(rstate, out_dir, rv.ReviewSettings())
        records = rstate.records()
    with ExtractionState(state_dir / "extract.sqlite3") as state:
        build_extractor(state, adapter).run(sorted(APPDETAILS))
        counts = export(state, out_dir, records)
    for name in ("games", "provenance", "prices", "tag_votes", "early_access", "failed", "review_summaries"):
        (out_dir / f"{name}.csv").rename(out_dir / f"{name}.sample.csv")
    (out_dir / "review_summaries.meta.json").rename(out_dir / "review_summaries.meta.sample.json")
    return counts


if __name__ == "__main__":
    import tempfile

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        print(write_samples(SAMPLES_DIR, Path(tmp)))

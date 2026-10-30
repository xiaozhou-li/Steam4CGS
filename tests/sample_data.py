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

REVIEWS = {
    9900001: {"review_score": 8, "review_score_desc": "Very Positive", "total_positive": 1100,
              "total_negative": 134, "total_reviews": 1234},
    9900002: {"review_score": 0, "review_score_desc": "3 user reviews", "total_positive": 3,
              "total_negative": 0, "total_reviews": 3},
    9900003: {"review_score": 0, "review_score_desc": "No user reviews", "total_positive": 0,
              "total_negative": 0, "total_reviews": 0},
    9900004: {"review_score": 6, "review_score_desc": "Mostly Positive", "total_positive": 70,
              "total_negative": 20, "total_reviews": 90},
}

STORE_ITEM_TAGS = {
    9900001: [{"tagid": 492, "weight": 300}, {"tagid": 19, "weight": 250}, {"tagid": 999999, "weight": 200},
              {"tagid": 7208, "weight": 150}, {"tagid": 21, "weight": 150}],
    9900002: [{"tagid": 493, "weight": 90}, {"tagid": 1662, "weight": 80}, {"tagid": 492, "weight": 10}],
    9900003: [],
    9900004: 500,  # HTTP 500 -> tags source unavailable
}


class SampleSteam(BaseAdapter):
    """Serves the synthetic payloads above for the four endpoints the extractor uses."""

    def __init__(self, appdetails=None, reviews=None, tags=None, tag_names=None):
        super().__init__()
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

    def send(self, request, **kwargs):
        url = urlsplit(request.url)
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}
        self.calls.append((url.path, params))
        if url.path == "/api/appdetails":
            appid = int(params["appids"])
            data = self.appdetails.get(appid)
            entry = {"success": True, "data": data} if data is not None else {"success": False}
            return self._respond(request, 200, {str(appid): entry})
        if url.path.startswith("/appreviews/"):
            appid = int(url.path.rsplit("/", 1)[1])
            summary = self.reviews.get(appid)
            if isinstance(summary, int):
                return self._respond(request, summary, {})
            return self._respond(request, 200, {"success": 1, "query_summary": summary or {}, "reviews": []})
        if url.path == "/IStoreBrowseService/GetItems/v1/":
            appid = json.loads(params["input_json"])["ids"][0]["appid"]
            tags = self.tags.get(appid, [])
            if isinstance(tags, int):
                return self._respond(request, tags, {})
            item = {"item_type": 0, "id": appid, "success": 1, "visible": True, "appid": appid,
                    "tagids": [t["tagid"] for t in tags], "tags": tags}
            return self._respond(request, 200, {"response": {"store_items": [item]}})
        if url.path == "/IStoreService/GetTagList/v1/":
            if isinstance(self.tag_names, int):
                return self._respond(request, self.tag_names, {})
            return self._respond(request, 200, {"response": {"version_hash": "sample",
                                 "tags": [{"tagid": k, "name": v} for k, v in self.tag_names.items()]}})
        raise AssertionError(f"unexpected request {url.path}")

    def close(self):
        pass


def build_extractor(state, adapter: SampleSteam, legacy_ids=frozenset(), key=None):
    from fakes import make_transport

    from crawler.extract import Extractor
    from crawler.steam_api import SteamStoreAPI, SteamWebAPI
    from crawler.transport import RetryPolicy

    transport = make_transport(adapter, retry=RetryPolicy(max_attempts=1))
    return Extractor(state, web=SteamWebAPI(transport, key), store=SteamStoreAPI(transport),
                     legacy_ids=frozenset(legacy_ids), now=lambda: FIXED_NOW, log=lambda _: None)


def write_samples(out_dir: Path, state_path: Path) -> dict[str, int]:
    from crawler.extract import ExtractionState, export

    with ExtractionState(state_path) as state:
        build_extractor(state, SampleSteam()).run(sorted(APPDETAILS))
        counts = export(state, out_dir)
    for name in ("games", "provenance", "prices", "failed"):
        (out_dir / f"{name}.csv").rename(out_dir / f"{name}.sample.csv")
    return counts


if __name__ == "__main__":
    import tempfile

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        print(write_samples(SAMPLES_DIR, Path(tmp) / "state.sqlite3"))

"""Game-level review summaries from ``IUserReviewsService/GetAppReviews/v1``.

One request per game, asking for the summary only (``num_per_page=0`` when the
method declares it); individual reviews are never paged through. Every
response is verified before its label is used:

* the counts must be non-negative ints with total = positive + negative;
* ``No user reviews`` requires 0 reviews, ``N user reviews`` requires exactly N
  (1-9), and a score label such as ``Very Positive`` requires 10 or more.

Only a response that passes becomes ``status=ok`` and fills the legacy
``sentiment`` column. Request parameters are restricted to those Steam
declares for the method (``ISteamWebAPIUtil/GetSupportedAPIList``), and the
requested, sent and unsupported settings are stored with every record.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crawler import schema
from crawler.discovery import write_csv_atomic
from crawler.discovery_state import iso
from crawler.steam_api import WEB_API_BASE, SteamWebAPI, public_url
from crawler.transport import RequestBudgetExceeded, SteamAuthError, SteamError, SteamRateLimitError

INTERFACE, METHOD, VERSION = "IUserReviewsService", "GetAppReviews", 1
ENDPOINT_URL = f"{WEB_API_BASE}/{INTERFACE}/{METHOD}/v{VERSION}/"

PURCHASE_TYPES = ("all", "steam", "non_steam_purchase")
NO_REVIEWS_DESC = "No user reviews"
LABEL_MIN_REVIEWS = 10  # Steam shows "N user reviews" instead of a score label below 10 reviews
_FEW_REVIEWS_RE = re.compile(r"([1-9]) user reviews")
_COUNT_FIELDS = ("total_positive", "total_negative", "total_reviews")

# Statuses
OK = "ok"                                   # verified; sentiment = review_score_desc
NO_REVIEWS = "no_reviews"                   # verified: total_reviews == 0
MISSING_LABEL = "missing_label"             # counts verified, but no review_score_desc
UNRECOGNIZED_LABEL = "unrecognized_label"   # counts verified, label outside the legacy vocabulary
INVALID_RESPONSE = "invalid_response"       # shape or consistency check failed
API_ERROR = "api_error"                     # request failed after retries; retried next invocation
STATUSES = (OK, NO_REVIEWS, MISSING_LABEL, UNRECOGNIZED_LABEL, INVALID_RESPONSE, API_ERROR)

SUMMARY_FIELDS = (
    "appid", "status", "sentiment", "review_score_desc", "review_score", "total_positive", "total_negative",
    "total_reviews", "review_language", "purchase_type", "params_sent", "params_not_supported", "source_url",
    "retrieved_at", "detail",
)


class ReviewEndpointUnavailable(Exception):
    """GetAppReviews is not visible to this key (or the capability list could not be read)."""


@dataclass(frozen=True)
class ReviewSettings:
    language: str = "all"
    purchase_type: str = "all"

    def __post_init__(self) -> None:
        if self.purchase_type not in PURCHASE_TYPES:
            raise ValueError(f"purchase_type must be one of {PURCHASE_TYPES}")
        if not self.language or not self.language.replace("_", "").isalnum():
            raise ValueError(f"invalid review language {self.language!r}")

    def desired_params(self) -> dict[str, Any]:
        # num_per_page=0: summary only, no individual reviews.
        return {"language": self.language, "purchase_type": self.purchase_type, "num_per_page": 0}


@dataclass(frozen=True)
class ReviewRecord:
    appid: int
    status: str
    sentiment: str                      # the legacy cell: non-empty only when status == ok
    review_score_desc: str | None
    review_score: int | None
    total_positive: int | None
    total_negative: int | None
    total_reviews: int | None
    review_language: str
    purchase_type: str
    params_sent: str                    # "k=v;..." exactly as sent (key excluded)
    params_not_supported: str           # requested settings the method does not declare
    source_url: str
    retrieved_at: str
    detail: str = ""

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> ReviewRecord:
        return cls(**json.loads(text))

    def as_row(self) -> dict[str, str]:
        return {k: "" if getattr(self, k) is None else str(getattr(self, k)) for k in SUMMARY_FIELDS}


# --------------------------------------------------------------------------
# Verification (pure)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Verified:
    status: str
    sentiment: str
    review_score_desc: str | None = None
    review_score: int | None = None
    total_positive: int | None = None
    total_negative: int | None = None
    total_reviews: int | None = None
    detail: str = ""


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _summary_object(response: Any) -> Mapping[str, Any] | None:
    """Locate the summary: ``response.query_summary`` or the flat ``response`` object."""
    if not isinstance(response, Mapping):
        return None
    nested = response.get("query_summary")
    if isinstance(nested, Mapping):
        return nested
    if any(k in response for k in (*_COUNT_FIELDS, "review_score_desc")):
        return response
    return None


def verify_summary(response: Any) -> Verified:
    """Check a GetAppReviews ``response`` object and decide the sentiment cell."""
    summary = _summary_object(response)
    if summary is None:
        keys = sorted(response) if isinstance(response, Mapping) else type(response).__name__
        return Verified(INVALID_RESPONSE, "", detail=f"no review summary in response (keys: {keys})")

    desc_raw = summary.get("review_score_desc")
    desc = desc_raw.strip() if isinstance(desc_raw, str) and desc_raw.strip() else None
    score = summary.get("review_score")
    score = score if isinstance(score, int) and not isinstance(score, bool) else None
    counts = {k: _count(summary.get(k)) for k in _COUNT_FIELDS}
    base = dict(review_score_desc=desc, review_score=score, **counts)

    bad = [k for k, v in counts.items() if v is None]
    if bad:
        return Verified(INVALID_RESPONSE, "", **base, detail=f"missing or invalid counts: {bad}")
    pos, neg, total = counts["total_positive"], counts["total_negative"], counts["total_reviews"]
    if pos + neg != total:
        return Verified(INVALID_RESPONSE, "", **base,
                        detail=f"total_positive + total_negative = {pos + neg} != total_reviews = {total}")

    few = _FEW_REVIEWS_RE.fullmatch(desc) if desc else None
    if total == 0:
        if desc in (None, NO_REVIEWS_DESC):
            return Verified(NO_REVIEWS, "", **base, detail="total_reviews = 0")
        return Verified(INVALID_RESPONSE, "", **base, detail=f"label {desc!r} with total_reviews = 0")
    if desc is None:
        return Verified(MISSING_LABEL, "", **base, detail=f"no review_score_desc for {total} review(s)")
    if desc == NO_REVIEWS_DESC:
        return Verified(INVALID_RESPONSE, "", **base, detail=f"{desc!r} with total_reviews = {total}")
    if few:
        if total < LABEL_MIN_REVIEWS and int(few.group(1)) == total:
            return Verified(OK, desc, **base)
        return Verified(INVALID_RESPONSE, "", **base, detail=f"label {desc!r} does not match total_reviews = {total}")
    if desc in schema.SENTIMENT_LABELS:
        if total >= LABEL_MIN_REVIEWS:
            return Verified(OK, desc, **base)
        return Verified(INVALID_RESPONSE, "", **base,
                        detail=f"score label {desc!r} needs >= {LABEL_MIN_REVIEWS} reviews, got {total}")
    return Verified(UNRECOGNIZED_LABEL, "", **base, detail=f"label {desc!r} is not in the legacy vocabulary")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


class ReviewState:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS summaries (appid INTEGER PRIMARY KEY, record_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> ReviewState:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def save(self, record: ReviewRecord) -> None:
        with self._db:
            self._db.execute("INSERT INTO summaries VALUES (?, ?) ON CONFLICT(appid) DO UPDATE SET "
                             "record_json = excluded.record_json", (record.appid, record.to_json()))

    def records(self) -> dict[int, ReviewRecord]:
        return {a: ReviewRecord.from_json(j) for a, j in self._db.execute("SELECT appid, record_json FROM summaries")}

    def set_meta(self, key: str, value: Any) -> None:
        with self._db:
            self._db.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                             (key, json.dumps(value, sort_keys=True)))

    def meta(self) -> dict[str, Any]:
        return {k: json.loads(v) for k, v in self._db.execute("SELECT key, value FROM meta")}


def load_records(path: str | Path) -> dict[int, ReviewRecord]:
    """Review records from a state file; empty when it does not exist yet."""
    if not Path(path).exists():
        return {}
    with ReviewState(path) as state:
        return state.records()


# --------------------------------------------------------------------------
# Collector
# --------------------------------------------------------------------------


@dataclass
class ReviewReport:
    requested: int = 0
    skipped_legacy: list[int] = field(default_factory=list)
    attempted: int = 0
    statuses: dict[str, int] = field(default_factory=dict)
    pending: int = 0
    stop_reason: str = "complete"


class ReviewCollector:
    def __init__(
        self,
        state: ReviewState,
        web: SteamWebAPI,
        *,
        settings: ReviewSettings = ReviewSettings(),
        legacy_ids: frozenset[int] = frozenset(),
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
        log: Callable[[str], None] = print,
    ):
        self.state = state
        self.web = web
        self.settings = settings
        self.legacy_ids = legacy_ids
        self._now = now
        self._log = log
        self._declared: frozenset[str] | None = None

    def check_capability(self) -> frozenset[str]:
        """Confirm GetAppReviews is visible to this key and return its declared parameter names."""
        try:
            methods = self.web.get_supported_methods()
        except SteamError as exc:
            raise ReviewEndpointUnavailable(f"could not read GetSupportedAPIList: {exc}") from None
        method = methods.get((INTERFACE, METHOD, VERSION))
        if method is None:
            hint = "" if self.web.has_key else " (no STEAM_API_KEY set: only public methods are listed)"
            raise ReviewEndpointUnavailable(f"{INTERFACE}/{METHOD}/v{VERSION} is not available to this key{hint}")
        self._declared = frozenset(method.parameters)
        self.state.set_meta("declared_parameters", sorted(self._declared))
        self.state.set_meta("http_method", method.http_method)
        return self._declared

    def _params(self) -> tuple[dict[str, Any], list[str]]:
        assert self._declared is not None, "check_capability() must run first"
        desired = self.settings.desired_params()
        sent = {k: v for k, v in desired.items() if k in self._declared}
        return sent, sorted(set(desired) - set(sent))

    def collect_one(self, appid: int) -> ReviewRecord:
        params, unsupported = self._params()
        sent_text = ";".join(f"{k}={v}" for k, v in sorted({"appid": appid, **params}.items()))
        url = public_url(ENDPOINT_URL, {"appid": appid, **params})
        common = dict(appid=appid, review_language=self.settings.language, purchase_type=self.settings.purchase_type,
                      params_sent=sent_text, params_not_supported=";".join(unsupported), source_url=url)
        try:
            response = self.web.get_app_reviews(appid, **params)
        except (SteamRateLimitError, RequestBudgetExceeded, SteamAuthError):
            raise
        except SteamError as exc:
            return ReviewRecord(status=API_ERROR, sentiment="", review_score_desc=None, review_score=None,
                                total_positive=None, total_negative=None, total_reviews=None,
                                retrieved_at=iso(self._now()), detail=f"{type(exc).__name__}: {exc}", **common)
        v = verify_summary(response)
        return ReviewRecord(status=v.status, sentiment=v.sentiment, review_score_desc=v.review_score_desc,
                            review_score=v.review_score, total_positive=v.total_positive,
                            total_negative=v.total_negative, total_reviews=v.total_reviews,
                            retrieved_at=iso(self._now()), detail=v.detail, **common)

    def run(self, appids: Iterable[int], *, limit: int | None = None, refresh: bool = False) -> ReviewReport:
        report = ReviewReport()
        wanted = list(dict.fromkeys(appids))
        report.requested = len(wanted)
        report.skipped_legacy = [a for a in wanted if a in self.legacy_ids]
        candidates = [a for a in wanted if a not in self.legacy_ids]
        existing = self.state.records()

        def needed(appid: int) -> bool:
            r = existing.get(appid)
            return (refresh or r is None or r.status == API_ERROR
                    or (r.review_language, r.purchase_type) != (self.settings.language, self.settings.purchase_type))

        todo = [a for a in candidates if needed(a)]
        self._log(f"{len(todo)} game(s) need a review summary")
        if todo:
            self.check_capability()
        for appid in todo:
            if limit is not None and report.attempted >= limit:
                report.stop_reason = "limit_reached"
                break
            try:
                record = self.collect_one(appid)
            except SteamRateLimitError as exc:
                report.stop_reason = "rate_limited"
                self._log(f"stopping: {exc}")
                break
            except RequestBudgetExceeded:
                report.stop_reason = "budget_exhausted"
                break
            report.attempted += 1
            self.state.save(record)

        records = self.state.records()
        report.statuses = {s: sum(1 for a in candidates if a in records and records[a].status == s) for s in STATUSES}
        report.pending = sum(1 for a in candidates if a not in records)
        return report


def export(state: ReviewState, out_dir: str | Path, settings: ReviewSettings) -> dict[str, int]:
    """Write review_summaries.csv (numeric statistics) and review_summaries.meta.json."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    records = state.records()
    write_csv_atomic(out / "review_summaries.csv", SUMMARY_FIELDS, (records[a].as_row() for a in sorted(records)))
    meta = {
        "endpoint": f"{INTERFACE}/{METHOD}/v{VERSION}",
        "settings": dataclasses.asdict(settings),
        "requested_params": settings.desired_params(),
        **state.meta(),
        "statuses": {s: sum(r.status == s for r in records.values()) for s in STATUSES},
    }
    tmp = out / ".review_summaries.meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(out / "review_summaries.meta.json")
    return {"review_summaries": len(records)}

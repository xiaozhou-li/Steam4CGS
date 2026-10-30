"""Normalize Steam Store sources into one legacy-format row plus per-field provenance.

``normalize`` is pure: it takes already-fetched source payloads and returns a
validated :class:`crawler.schema.GameRecord` row, one provenance record per
column, and extra datasets kept outside the 13 legacy columns (tag vote counts,
Early Access evidence). It never substitutes one source for another (no genres
as tags), takes ``sentiment`` only from a verified GetAppReviews record, reports
Early Access as unknown unless the Store says so explicitly, and prices from the
regular listing price only. The mapping rules are documented in docs/metadata.md.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from crawler import reviews as rv
from crawler import schema
from crawler import steamspy as spy
from crawler.release_dates import parse_release_date
from crawler.steam_api import MAX_TAGS, store_item_tag_weights
from crawler.transport import SteamResponseError

# Provenance statuses
PRESENT = "present"                        # value taken from the source
MISSING = "missing"                        # source answered, but has no value for this field
UNVERIFIED = "unverified"                  # the evidence needed to decide is missing
UNRECOGNIZED = "unrecognized"              # source value exists but does not fit the contract
SOURCE_UNAVAILABLE = "source_unavailable"  # the request for this source failed
NOT_COLLECTED = "not_collected"            # no review summary has been collected for this app yet

# Review-summary status -> provenance status for the sentiment column.
_REVIEW_STATUS = {
    rv.OK: PRESENT, rv.NO_REVIEWS: MISSING, rv.MISSING_LABEL: MISSING,
    rv.UNRECOGNIZED_LABEL: UNRECOGNIZED, rv.INVALID_RESPONSE: UNRECOGNIZED, rv.API_ERROR: SOURCE_UNAVAILABLE,
}

EXPECTED_CURRENCY = "USD"
PRICE_CONVERSION = (
    "price_overview.initial (regular listing price, USD cents) rounded to whole dollars, halves up; "
    "is_free=true -> 0; sale prices (final with discount_percent > 0) are never used"
)

# The order in which spec labels appear in steamproducts2020.csv. It is
# consistent across all 23,034 rows (tests/test_metadata.py re-derives it).
# Store categories arrive in a different order, so they are re-sorted;
# categories absent from the legacy file keep Steam's order after these.
SPEC_ORDER: tuple[str, ...] = (
    "Single-player", "Multi-player", "MMO", "Online PvP", "LAN PvP", "Shared/Split Screen PvP",
    "Online Co-op", "LAN Co-op", "Shared/Split Screen Co-op", "Cross-Platform Multiplayer",
    "Steam Achievements", "Full controller support", "Steam Trading Cards", "Captions available",
    "Steam Workshop", "In-App Purchases", "Partial Controller Support", "Steam Cloud",
    "Valve Anti-Cheat enabled", "Stats", "Steam Leaderboards", "Steam Turn Notifications",
    "Includes level editor", "Includes Source SDK", "Commentary available", "Remote Play on Phone",
    "Remote Play on Tablet", "Remote Play on TV", "Remote Play Together",
)
_SPEC_RANK = {label: i for i, label in enumerate(SPEC_ORDER)}

PROVENANCE_FIELDS = ("appid", "field", "value", "status", "source", "source_url", "retrieved_at", "raw", "note")

# Tag sources (extract --tags-source)
TAGS_STEAM = "steam"        # IStoreBrowseService/GetItems tag ids + GetTagList names (default)
TAGS_STEAMSPY = "steamspy"  # SteamSpy appdetails tag -> votes (optional, third party)
TAG_SOURCES = (TAGS_STEAM, TAGS_STEAMSPY)

# Early Access status
EA_TRUE, EA_FALSE, EA_UNKNOWN = "true", "false", "unknown"


class ExtractionError(Exception):
    """A row cannot be built (no store details, AppID mismatch, required field missing)."""


@dataclass(frozen=True)
class SourceResult:
    """One fetched source. ``data`` is the parsed payload; ``error`` is set when the request failed."""

    name: str
    url: str
    retrieved_at: str | None
    data: Any = None
    error: str | None = None
    cached: bool = False  # served from a local cache; retrieved_at is the original fetch time

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class FieldProvenance:
    appid: int
    field: str
    value: str
    status: str
    source: str
    source_url: str
    retrieved_at: str
    raw: str
    note: str = ""

    def as_row(self) -> dict[str, str]:
        return {k: str(getattr(self, k)) for k in PROVENANCE_FIELDS}


@dataclass(frozen=True)
class Extracted:
    record: schema.GameRecord
    row: dict[str, str]
    provenance: tuple[FieldProvenance, ...]
    complete: bool  # False when any source was unavailable (worth retrying later)
    extras: dict = field(default_factory=dict)  # fields kept outside the 13 legacy columns


def _raw(value: Any) -> str:
    return "" if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _html_text(text: str) -> str:
    """Legacy HTML-derived fields store '&', '<', '>' escaped; normalize either input form to that."""
    return html.escape(html.unescape(text.strip()), quote=False)


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        if isinstance(v, str) and v.strip() and v.strip() not in out:
            out.append(v.strip())
    return out


@dataclass
class _Builder:
    appid: int
    row: dict[str, str] = field(default_factory=dict)
    provenance: list[FieldProvenance] = field(default_factory=list)

    def set(self, column: str, value: str, status: str, src: SourceResult, path: str, raw: Any = None,
            note: str = "") -> None:
        self.row[column] = value
        self.provenance.append(FieldProvenance(
            self.appid, column, value, status, f"{src.name}:{path}" if path else src.name, src.url,
            src.retrieved_at or "", _raw(raw), note))


def normalize(
    appid: int,
    appdetails: SourceResult,
    tags: SourceResult,
    tag_names: Mapping[int, str] | None,
    review: rv.ReviewRecord | None = None,
    store_item: SourceResult | None = None,
) -> Extracted:
    """Build one row.

    ``tags`` is either a ``store_items`` source (data: list of tag ids, names from
    ``tag_names``) or a ``steamspy`` source (data: SteamSpy appdetails object).
    ``store_item`` is the GetItems store item used as Early Access evidence.
    """
    if not appdetails.ok:
        raise ExtractionError(f"appdetails unavailable: {appdetails.error}")
    data = appdetails.data
    if not isinstance(data, Mapping):
        raise ExtractionError("appdetails returned success=false (no store details)")
    steam_appid = data.get("steam_appid")
    if isinstance(steam_appid, int) and steam_appid != appid:
        raise ExtractionError(f"appdetails answered for steam_appid={steam_appid}, not {appid}")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ExtractionError("appdetails has no name")

    b = _Builder(appid)
    ad = appdetails

    # id, app_name, title -------------------------------------------------------
    b.set("id", str(appid), PRESENT, ad, "steam_appid", steam_appid)
    app_name = html.unescape(name.strip())
    b.set("app_name", app_name, PRESENT, ad, "name", name)
    b.set("title", schema.title_from_app_name(app_name), PRESENT, ad, "name", name,
          "html.escape(app_name, quote=False)")

    # genres + early_access -----------------------------------------------------------
    raw_genres = data.get("genres")
    descriptions = _strings([g.get("description") for g in raw_genres if isinstance(g, dict)]
                            if isinstance(raw_genres, list) else None)
    genres = [_html_text(g) for g in descriptions]
    genres = sorted((g for g in genres if g != schema.EARLY_ACCESS_GENRE), key=str.casefold) + (
        [schema.EARLY_ACCESS_GENRE] if schema.EARLY_ACCESS_GENRE in genres else [])
    if genres:
        b.set("genres", schema.encode_list(genres), PRESENT, ad, "genres[].description", raw_genres,
              "sorted case-insensitively, 'Early Access' last (legacy order)")
    else:
        b.set("genres", "", MISSING, ad, "genres", raw_genres)
    ea = early_access_evidence(appid, genres if genres else None, ad, store_item)
    b.provenance.append(ea.provenance)
    b.row["early_access"] = ea.provenance.value

    # developer / publisher ------------------------------------------------------------------
    for column, key in (("developer", "developers"), ("publisher", "publishers")):
        names = [_html_text(n) for n in _strings(data.get(key))]
        note = "joined with ', '"
        if column == "publisher":
            note += "; appdetails has no franchise, so the legacy 'Franchise:' suffix is never added"
        if names:
            b.set(column, ", ".join(names), PRESENT, ad, key, data.get(key), note)
        else:
            b.set(column, "", MISSING, ad, key, data.get(key))

    # release_date ----------------------------------------------------------------------------
    release = data.get("release_date") if isinstance(data.get("release_date"), Mapping) else {}
    raw_date = release.get("date") if isinstance(release.get("date"), str) else ""
    parsed = parse_release_date(raw_date)
    soon = " coming_soon=true" if release.get("coming_soon") is True else ""
    if parsed is not None and parsed.exact is not None:
        b.set("release_date", schema.encode_release_date(parsed.exact), PRESENT, ad, "release_date.date",
              release, f"exact date reformatted as 'D Mon, YYYY'{soon}")
    elif raw_date.strip():
        precision = parsed.precision if parsed else "unparsed"
        b.set("release_date", raw_date.strip(), PRESENT, ad, "release_date.date", release,
              f"kept verbatim; precision={precision}, no day invented{soon}")
    else:
        raise ExtractionError("appdetails has no release_date.date")

    # specs -------------------------------------------------------------------------------------
    raw_categories = data.get("categories")
    labels = _strings([c.get("description") for c in raw_categories if isinstance(c, dict)]
                      if isinstance(raw_categories, list) else None)
    known = sorted((l for l in labels if l in _SPEC_RANK), key=_SPEC_RANK.__getitem__)
    specs = known + [l for l in labels if l not in _SPEC_RANK]
    if specs:
        new = [l for l in labels if l not in _SPEC_RANK]
        b.set("specs", schema.encode_list(specs), PRESENT, ad, "categories[].description", raw_categories,
              "legacy label order" + (f"; not in legacy vocabulary: {new}" if new else ""))
    else:
        b.set("specs", "", MISSING, ad, "categories", raw_categories)

    # tags ----------------------------------------------------------------------------------------
    if tags.name == TAGS_STEAMSPY:
        tag_votes = _map_steamspy_tags(b, tags)
    else:
        tag_votes = _map_tags(b, tags, tag_names, store_item)

    # price -----------------------------------------------------------------------------------------
    _map_price(b, ad, data)

    # sentiment ---------------------------------------------------------------------------------------
    b.provenance.append(sentiment_field(appid, review))
    b.row["sentiment"] = b.provenance[-1].value

    # metascore ----------------------------------------------------------------------------------------
    metacritic = data.get("metacritic")
    score = metacritic.get("score") if isinstance(metacritic, Mapping) else None
    if isinstance(score, int) and not isinstance(score, bool) and 0 <= score <= 100:
        b.set("metascore", str(score), PRESENT, ad, "metacritic.score", metacritic)
    elif metacritic is None:
        b.set("metascore", "", MISSING, ad, "metacritic")
    else:
        b.set("metascore", "", UNRECOGNIZED, ad, "metacritic.score", metacritic, "score is not an int in 0..100")

    row = {c: b.row[c] for c in schema.COLUMNS}
    try:
        record = schema.GameRecord.from_row(row)
    except schema.SchemaError as exc:
        raise ExtractionError(f"normalized row violates the schema: {exc}") from None
    by_field = {p.field: p for p in b.provenance}
    provenance = tuple(by_field[c] for c in schema.COLUMNS)
    # Review summaries are collected separately (python -m crawler reviews), so they don't make a row partial.
    complete = all(p.status != SOURCE_UNAVAILABLE for p in provenance if p.field != "sentiment")
    extras = {
        "tags_source": TAGS_STEAMSPY if tags.name == TAGS_STEAMSPY else TAGS_STEAM,
        "tag_votes": tag_votes,
        "early_access": ea.as_row(),
    }
    return Extracted(record, row, provenance, complete, extras)


def _tag_vote_rows(src: SourceResult, ranked: Sequence[tuple[str, int | None]], kind: str) -> list[dict[str, str]]:
    return [{"rank": str(i), "tag": name, "count": "" if count is None else str(count), "count_kind": kind,
             "source": src.name, "source_url": src.url, "retrieved_at": src.retrieved_at or ""}
            for i, (name, count) in enumerate(ranked, 1)]


def _map_tags(b: _Builder, tags: SourceResult, tag_names: Mapping[int, str] | None,
              store_item: SourceResult | None) -> list[dict[str, str]]:
    if not tags.ok:
        b.set("tags", "", SOURCE_UNAVAILABLE, tags, "", None, tags.error or "")
        return []
    if tag_names is None:
        b.set("tags", "", SOURCE_UNAVAILABLE, tags, "tagids", tags.data, "tag names (GetTagList) unavailable")
        return []
    if tags.data is None:
        b.set("tags", "", MISSING, tags, "store_items", None, "no visible store item")
        return []
    tagids: Sequence[int] = tags.data
    weights: dict[int, int | None] = {}
    if store_item is not None and store_item.ok and isinstance(store_item.data, Mapping):
        try:
            weights = dict(store_item_tag_weights(store_item.data))
        except SteamResponseError:
            weights = {}
    ranked = [(tag_names[t], weights.get(t)) for t in tagids if t in tag_names][:MAX_TAGS]
    unknown = [t for t in tagids if t not in tag_names]
    note = "Steam order (most votes first); genres are never used as tags"
    if unknown:
        note += f"; unknown tag ids skipped: {unknown}"
    if ranked:
        b.set("tags", schema.encode_list([n for n, _ in ranked]), PRESENT, tags, "tags[].tagid", list(tagids), note)
        return _tag_vote_rows(tags, ranked, "steam_tag_weight")
    b.set("tags", "", MISSING, tags, "tags", list(tagids), "no user tags" if not tagids else note)
    return []


def _map_steamspy_tags(b: _Builder, src: SourceResult) -> list[dict[str, str]]:
    cache_note = "; served from cache (retrieved_at is the original fetch)" if src.cached else ""
    if not src.ok:
        b.set("tags", "", SOURCE_UNAVAILABLE, src, "", None, (src.error or "") + "; genres are not substituted")
        return []
    raw_tags = src.data.get("tags") if isinstance(src.data, Mapping) else None
    try:
        ranked = spy.parse_tags(b.appid, src.data)
    except spy.SteamSpyFormatError as exc:
        b.set("tags", "", UNRECOGNIZED, src, "tags", raw_tags, f"{exc}; genres are not substituted{cache_note}")
        return []
    if ranked is None:
        b.set("tags", "", MISSING, src, "name", None, f"SteamSpy has no record for this app{cache_note}")
        return []
    if not ranked:
        b.set("tags", "", MISSING, src, "tags", raw_tags, f"SteamSpy lists no tags; genres are not substituted{cache_note}")
        return []
    top = ranked[:MAX_TAGS]
    b.set("tags", schema.encode_list([n for n, _ in top]), PRESENT, src, "tags", raw_tags,
          f"top {len(top)} of {len(ranked)} by votes desc, ties by name; vote counts in tag_votes.csv{cache_note}")
    return _tag_vote_rows(src, top, "steamspy_votes")


@dataclass(frozen=True)
class EarlyAccess:
    status: str           # true | false | unknown
    reason: str
    store_item_flag: str  # true | false | absent | no_store_item | unavailable | not_requested
    genre: str            # present | absent | no_genres
    provenance: FieldProvenance
    store_item_url: str
    store_item_retrieved_at: str
    appdetails_url: str
    appdetails_retrieved_at: str

    def as_row(self) -> dict[str, str]:
        return {"early_access": self.provenance.value, "status": self.status, "reason": self.reason,
                "store_item_is_early_access": self.store_item_flag, "genres_early_access": self.genre,
                "store_item_url": self.store_item_url, "store_item_retrieved_at": self.store_item_retrieved_at,
                "appdetails_url": self.appdetails_url, "appdetails_retrieved_at": self.appdetails_retrieved_at}


def early_access_evidence(appid: int, genres: Sequence[str] | None, appdetails: SourceResult,
                          store_item: SourceResult | None) -> EarlyAccess:
    """TRUE / FALSE / unknown from current Store metadata; absence of evidence is never FALSE.

    Evidence: the GetItems store item's explicit ``is_early_access`` flag and
    the ``Early Access`` genre in appdetails. User tags are never consulted.
    """
    genre = "no_genres" if not genres else ("present" if schema.EARLY_ACCESS_GENRE in genres else "absent")
    if store_item is None:
        flag = "not_requested"
    elif not store_item.ok:
        flag = "unavailable"
    elif not isinstance(store_item.data, Mapping):
        flag = "no_store_item"
    else:
        value = store_item.data.get("is_early_access")
        flag = ("true" if value else "false") if isinstance(value, bool) else "absent"

    if flag == "true":
        status, reason = ((EA_UNKNOWN, "conflict: store item is_early_access=true but genres lack 'Early Access'")
                          if genre == "absent" else (EA_TRUE, "store item is_early_access=true"))
    elif flag == "false":
        status, reason = ((EA_UNKNOWN, "conflict: store item is_early_access=false but genres include 'Early Access'")
                          if genre == "present" else (EA_FALSE, "store item is_early_access=false"))
    elif genre == "present":
        status, reason = EA_TRUE, f"'Early Access' genre present; store item flag {flag}"
    elif genre == "absent":
        status, reason = EA_UNKNOWN, (f"no explicit store flag (store item: {flag}); absence of the "
                                      "'Early Access' genre is not treated as FALSE")
    else:
        status, reason = EA_UNKNOWN, f"no explicit store flag (store item: {flag}) and no genres"

    cell = {EA_TRUE: "TRUE", EA_FALSE: "FALSE", EA_UNKNOWN: ""}[status]
    if status != EA_UNKNOWN:
        prov_status = PRESENT
    elif flag == "unavailable":
        prov_status = SOURCE_UNAVAILABLE  # retry later: the deciding source failed
    else:
        prov_status = UNVERIFIED
    item_url = store_item.url if store_item is not None else ""
    item_at = (store_item.retrieved_at or "") if store_item is not None else ""
    raw = {"store_item.is_early_access": flag, "genres": genre}
    prov = FieldProvenance(appid, "early_access", cell, prov_status,
                           "store_items:is_early_access+appdetails:genres", item_url or appdetails.url,
                           item_at or (appdetails.retrieved_at or ""), _raw(raw),
                           reason + "; user tags are never used")
    return EarlyAccess(status, reason, flag, genre, prov, item_url, item_at, appdetails.url,
                       appdetails.retrieved_at or "")


def _map_price(b: _Builder, ad: SourceResult, data: Mapping[str, Any]) -> None:
    po = data.get("price_overview")
    raw = {"is_free": data.get("is_free"), "price_overview": po}
    if data.get("is_free") is True:
        b.set("price", "0", PRESENT, ad, "is_free", raw, PRICE_CONVERSION)
        return
    if not isinstance(po, Mapping):
        b.set("price", "", MISSING, ad, "price_overview", raw,
              "no price_overview and is_free is not true (not purchasable or price hidden)")
        return
    currency = po.get("currency")
    if currency != EXPECTED_CURRENCY:
        b.set("price", "", UNRECOGNIZED, ad, "price_overview.currency", raw,
              f"currency {currency!r}, expected {EXPECTED_CURRENCY}; not converted")
        return
    initial, final, discount = po.get("initial"), po.get("final"), po.get("discount_percent", 0)
    if isinstance(initial, int) and not isinstance(initial, bool) and initial >= 0:
        cents, path = initial, "price_overview.initial"
    elif discount == 0 and isinstance(final, int) and not isinstance(final, bool) and final >= 0:
        cents, path = final, "price_overview.final"  # not on sale, so final is the regular price
    else:
        b.set("price", "", MISSING, ad, "price_overview.initial", raw,
              "regular price missing and the item is on sale; the sale price is not used")
        return
    note = f"regular price {cents} USD cents"
    if isinstance(discount, int) and discount > 0:
        note += f"; on sale at {final} cents (-{discount}%), sale price ignored"
    b.set("price", str(schema.price_from_cents(cents)), PRESENT, ad, path, raw, f"{note}; {PRICE_CONVERSION}")


def sentiment_field(appid: int, review: rv.ReviewRecord | None) -> FieldProvenance:
    """The sentiment cell and its provenance, taken only from a verified GetAppReviews record."""
    if review is None:
        return FieldProvenance(appid, "sentiment", "", NOT_COLLECTED, f"user_reviews:{rv.METHOD}", rv.ENDPOINT_URL, "",
                               "", "no review summary collected; run: python -m crawler reviews")
    raw = {k: getattr(review, k) for k in ("review_score_desc", "review_score", "total_positive",
                                           "total_negative", "total_reviews")}
    note = (f"status={review.status}; review_language={review.review_language}; "
            f"purchase_type={review.purchase_type}")
    if review.params_not_supported:
        note += f"; not supported by the method: {review.params_not_supported}"
    if review.detail:
        note += f"; {review.detail}"
    value = review.sentiment if review.status == rv.OK else ""
    return FieldProvenance(appid, "sentiment", value, _REVIEW_STATUS[review.status],
                           f"user_reviews:{rv.METHOD}.review_score_desc", review.source_url, review.retrieved_at,
                           _raw(raw), note)


def with_review(row: Mapping[str, str], provenance: Sequence[Mapping[str, str]],
                review: rv.ReviewRecord | None) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Replace a stored row's sentiment (cell + provenance) with the latest review record."""
    field_ = sentiment_field(int(row["id"]), review)
    new_row = dict(row, sentiment=field_.value)
    new_prov = [field_.as_row() if p["field"] == "sentiment" else dict(p) for p in provenance]
    return new_row, new_prov

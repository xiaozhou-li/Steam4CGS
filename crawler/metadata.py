"""Normalize Steam Store sources into one legacy-format row plus per-field provenance.

``normalize`` is pure: it takes already-fetched source payloads and returns a
validated :class:`crawler.schema.GameRecord` row and one provenance record per
column. It never substitutes one source for another (no genres as tags), never
derives a review label, and prices from the regular listing price only. The
mapping rules are documented in docs/metadata.md.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from crawler import schema
from crawler.release_dates import parse_release_date
from crawler.steam_api import MAX_TAGS

# Provenance statuses
PRESENT = "present"                        # value taken from the source
MISSING = "missing"                        # source answered, but has no value for this field
UNVERIFIED = "unverified"                  # the evidence needed to decide is missing
UNRECOGNIZED = "unrecognized"              # source value exists but does not fit the contract
SOURCE_UNAVAILABLE = "source_unavailable"  # the request for this source failed

EXPECTED_CURRENCY = "USD"
PRICE_CONVERSION = (
    "price_overview.initial (regular listing price, USD cents) rounded to whole dollars, halves up; "
    "is_free=true -> 0; sale prices (final with discount_percent > 0) are never used"
)
NO_REVIEWS_DESC = "No user reviews"

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
    reviews: SourceResult,
    tags: SourceResult,
    tag_names: Mapping[int, str] | None,
) -> Extracted:
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
        early = schema.EARLY_ACCESS_GENRE in genres
        b.set("early_access", schema.encode_bool(early), PRESENT, ad, "genres[].description", raw_genres,
              f"'Early Access' genre {'present' if early else 'absent'}")
    else:
        b.set("genres", "", MISSING, ad, "genres", raw_genres)
        b.set("early_access", "", UNVERIFIED, ad, "genres", raw_genres,
              "no genres, so the Early Access genre cannot be checked; user tags are not used")

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
    _map_tags(b, tags, tag_names)

    # price -----------------------------------------------------------------------------------------
    _map_price(b, ad, data)

    # sentiment ---------------------------------------------------------------------------------------
    _map_sentiment(b, reviews)

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
    complete = all(p.status != SOURCE_UNAVAILABLE for p in provenance)
    return Extracted(record, row, provenance, complete)


def _map_tags(b: _Builder, tags: SourceResult, tag_names: Mapping[int, str] | None) -> None:
    if not tags.ok:
        b.set("tags", "", SOURCE_UNAVAILABLE, tags, "", None, tags.error or "")
        return
    if tag_names is None:
        b.set("tags", "", SOURCE_UNAVAILABLE, tags, "tagids", tags.data, "tag names (GetTagList) unavailable")
        return
    if tags.data is None:
        b.set("tags", "", MISSING, tags, "store_items", None, "no visible store item")
        return
    tagids: Sequence[int] = tags.data
    names = [tag_names[t] for t in tagids if t in tag_names][:MAX_TAGS]
    unknown = [t for t in tagids if t not in tag_names]
    note = "Steam order (most votes first); genres are never used as tags"
    if unknown:
        note += f"; unknown tag ids skipped: {unknown}"
    if names:
        b.set("tags", schema.encode_list(names), PRESENT, tags, "tags[].tagid", list(tagids), note)
    else:
        b.set("tags", "", MISSING, tags, "tags", list(tagids), "no user tags" if not tagids else note)


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


def _map_sentiment(b: _Builder, reviews: SourceResult) -> None:
    if not reviews.ok:
        b.set("sentiment", "", SOURCE_UNAVAILABLE, reviews, "", None, reviews.error or "")
        return
    summary = reviews.data if isinstance(reviews.data, Mapping) else {}
    desc, total = summary.get("review_score_desc"), summary.get("total_reviews")
    raw = {k: summary.get(k) for k in ("review_score", "review_score_desc", "total_positive",
                                       "total_negative", "total_reviews")}
    try:
        label = schema.decode_sentiment(desc) if isinstance(desc, str) else None
    except ValueError:
        label = None
    if label:
        b.set("sentiment", label, PRESENT, reviews, "query_summary.review_score_desc", raw)
    elif desc == NO_REVIEWS_DESC or total == 0:
        b.set("sentiment", "", MISSING, reviews, "query_summary.review_score_desc", raw, "no user reviews")
    else:
        b.set("sentiment", "", UNRECOGNIZED, reviews, "query_summary.review_score_desc", raw,
              "label not in the legacy vocabulary; not guessed from counts")

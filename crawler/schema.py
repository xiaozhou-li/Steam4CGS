"""Schema contract for steamproducts2020.csv-compatible game metadata.

The legacy file ``steamproducts2020.csv`` is the reference. Every rule enforced
here holds for all 23,034 of its rows (see ``docs/legacy_schema.md`` and
``tests/test_legacy_csv.py``). New crawler output must be serialized through
this module so it stays byte-compatible with the legacy file.

Each column has a decoder (cell text -> Python value) and an encoder
(Python value -> cell text). Decoders double as validators: they raise
``ValueError`` for any cell that is not in canonical legacy form, so
``encode(decode(cell)) == cell`` for every valid cell.

This module is stdlib-only and performs no network I/O.
"""

from __future__ import annotations

import ast
import csv
import datetime as dt
import html
import io
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------
# File-level contract
# --------------------------------------------------------------------------

COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "genres",
    "developer",
    "publisher",
    "release_date",
    "app_name",
    "specs",
    "tags",
    "price",
    "sentiment",
    "early_access",
    "metascore",
)

ENCODING = "utf-8"  # no BOM
LINE_TERMINATOR = "\r\n"
UTF8_BOM = b"\xef\xbb\xbf"

# Required columns never have an empty cell; the rest use "" for missing.
# developer, specs, price and early_access are never empty in the legacy file,
# but new rows leave them blank rather than invent a value when the store
# omits them (see docs/metadata.md); missingness is recorded in provenance.
REQUIRED_COLUMNS: frozenset[str] = frozenset({"id", "title", "release_date", "app_name"})
NULLABLE_COLUMNS: frozenset[str] = frozenset(COLUMNS) - REQUIRED_COLUMNS

# --------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------

TRUE = "TRUE"
FALSE = "FALSE"

EARLY_ACCESS_GENRE = "Early Access"
FRANCHISE_SEPARATOR = "Franchise:"

# Steam review-summary labels, most to least favourable.
SENTIMENT_LABELS: tuple[str, ...] = (
    "Overwhelmingly Positive",
    "Very Positive",
    "Positive",
    "Mostly Positive",
    "Mixed",
    "Mostly Negative",
    "Negative",
    "Very Negative",
    "Overwhelmingly Negative",
)
# Steam shows a count instead of a label below 10 reviews ("1 user reviews" is
# plural in the legacy data and must stay that way).
_FEW_REVIEWS_RE = re.compile(r"[1-9] user reviews")

# Store-page notices that the legacy scraper captured with a trailing NBSP and
# followed by two empty strings: [..., NOTICE, '', ''].
SPECS_NOTICES: tuple[str, ...] = (
    "Profile Features Limited\xa0",
    "Steam is learning about this game\xa0",
)

# Locale-independent English month abbreviations (strftime's %b is locale-dependent).
MONTHS: tuple[str, ...] = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
_RELEASE_DATE_RE = re.compile(r"([1-9]|[12]\d|3[01]) (%s), (\d{4})" % "|".join(MONTHS))
# Anything shaped like "<digits> <word>, <digits>" must be a canonical, real date;
# other text is accepted as a free-text placeholder ("TBA", "Q1 2020", ...).
_DATE_LIKE_RE = re.compile(r"\d+ \w+, \d+")

_UINT_RE = re.compile(r"0|[1-9]\d*")

METASCORE_RANGE = (0, 100)


# --------------------------------------------------------------------------
# Cell codecs
# --------------------------------------------------------------------------


def encode_uint(value: int) -> str:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"expected a non-negative int, got {value!r}")
    return str(value)


def decode_uint(cell: str) -> int:
    if not _UINT_RE.fullmatch(cell):
        raise ValueError(f"expected a non-negative decimal integer without leading zeros, got {cell!r}")
    return int(cell)


def encode_bool(value: bool) -> str:
    if not isinstance(value, bool):
        raise ValueError(f"expected a bool, got {value!r}")
    return TRUE if value else FALSE


def decode_bool(cell: str) -> bool:
    if cell == TRUE:
        return True
    if cell == FALSE:
        return False
    raise ValueError(f"expected {TRUE!r} or {FALSE!r}, got {cell!r}")


def encode_list(values: Sequence[str] | None) -> str:
    """Encode a string list as its Python ``repr``; ``None`` or empty -> ``""``."""
    if not values:
        return ""
    if isinstance(values, str) or not all(isinstance(v, str) for v in values):
        raise ValueError(f"expected a sequence of str, got {values!r}")
    return repr(list(values))


def decode_list(cell: str) -> list[str] | None:
    """Decode a ``repr``-encoded string list; ``""`` -> ``None``.

    Uses ``ast.literal_eval`` (never ``eval``) and requires the cell to be the
    exact canonical ``repr`` of the decoded list.
    """
    if cell == "":
        return None
    if not (cell.startswith("[") and cell.endswith("]")):
        raise ValueError(f"expected a Python list literal, got {cell!r}")
    try:
        value = ast.literal_eval(cell)
    except (ValueError, SyntaxError) as exc:
        raise ValueError(f"unparseable list literal {cell!r}") from exc
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"expected a list of str, got {cell!r}")
    if not value:
        raise ValueError("an empty list must be encoded as an empty cell, not '[]'")
    if repr(value) != cell:
        raise ValueError(f"list is not in canonical repr form: {cell!r} (expected {value!r})")
    return value


def encode_release_date(value: dt.date | str) -> str:
    """Format a date as ``'3 Dec, 2019'``; free-text placeholders pass through."""
    if isinstance(value, dt.date):
        cell = f"{value.day} {MONTHS[value.month - 1]}, {value.year}"
    else:
        cell = value
    decode_release_date(cell)
    return cell


def decode_release_date(cell: str) -> str:
    """Validate a release-date cell and return it unchanged (it may be free text)."""
    if cell == "" or cell != cell.strip():
        raise ValueError(f"release_date must be non-empty without surrounding whitespace, got {cell!r}")
    if _DATE_LIKE_RE.fullmatch(cell):
        parse_release_date(cell, strict=True)
    return cell


def parse_release_date(cell: str, *, strict: bool = False) -> dt.date | None:
    """Return the date for a canonical ``'D Mon, YYYY'`` cell, else ``None``.

    With ``strict=True`` a non-canonical or impossible date raises ``ValueError``.
    """
    m = _RELEASE_DATE_RE.fullmatch(cell)
    if m:
        day, month, year = int(m.group(1)), MONTHS.index(m.group(2)) + 1, int(m.group(3))
        try:
            return dt.date(year, month, day)
        except ValueError:
            if strict:
                raise ValueError(f"impossible release date {cell!r}") from None
            return None
    if strict:
        raise ValueError(f"release date {cell!r} is not in canonical 'D Mon, YYYY' form")
    return None


def price_from_cents(cents: int) -> int:
    """Convert a price in minor units to the legacy whole-unit integer.

    Rounds to the nearest whole unit, halves up (3999 -> 40, 99 -> 1, 0 -> 0).
    See docs/legacy_schema.md for why rounding (not truncation) is inferred.
    """
    if isinstance(cents, bool) or not isinstance(cents, int) or cents < 0:
        raise ValueError(f"expected non-negative int cents, got {cents!r}")
    return (cents + 50) // 100


def encode_metascore(value: int | None) -> str:
    if value is None:
        return ""
    cell = encode_uint(value)
    decode_metascore(cell)
    return cell


def decode_metascore(cell: str) -> int | None:
    if cell == "":
        return None
    value = decode_uint(cell)
    lo, hi = METASCORE_RANGE
    if not lo <= value <= hi:
        raise ValueError(f"metascore must be within {lo}..{hi}, got {value}")
    return value


def encode_sentiment(value: str | None) -> str:
    cell = value or ""
    decode_sentiment(cell)
    return cell


def decode_sentiment(cell: str) -> str | None:
    if cell == "":
        return None
    if cell in SENTIMENT_LABELS or _FEW_REVIEWS_RE.fullmatch(cell):
        return cell
    raise ValueError(f"unknown sentiment {cell!r}")


def split_publisher(cell: str) -> tuple[str, str | None]:
    """Split ``'Paradox InteractiveFranchise:Cities: Skylines'`` into (publisher, franchise)."""
    publisher, sep, franchise = cell.partition(FRANCHISE_SEPARATOR)
    return publisher, (franchise if sep else None)


def join_publisher(publisher: str, franchise: str | None = None) -> str:
    """Inverse of :func:`split_publisher` (legacy concatenation has no space)."""
    return publisher if franchise is None else f"{publisher}{FRANCHISE_SEPARATOR}{franchise}"


def title_from_app_name(app_name: str) -> str:
    """Legacy ``title`` is ``app_name`` with ``&``, ``<``, ``>`` HTML-escaped."""
    return html.escape(app_name, quote=False)


# --------------------------------------------------------------------------
# Column-specific rules
# --------------------------------------------------------------------------


def _require_text(cell: str) -> str:
    if cell == "":
        raise ValueError("must not be empty")
    return cell


def _decode_id(cell: str) -> int:
    value = decode_uint(cell)
    if value == 0:
        raise ValueError("id must be a positive Steam app id")
    return value


def _optional_text(cell: str) -> str | None:
    return cell or None


def _decode_optional_bool(cell: str) -> bool | None:
    return None if cell == "" else decode_bool(cell)


def _decode_price(cell: str) -> int | None:
    return None if cell == "" else decode_uint(cell)


def _decode_publisher(cell: str) -> str | None:
    if cell == "":
        return None
    if cell.count(FRANCHISE_SEPARATOR) > 1:
        raise ValueError(f"at most one {FRANCHISE_SEPARATOR!r} suffix allowed, got {cell!r}")
    publisher, franchise = split_publisher(cell)
    if franchise is not None and (publisher == "" or franchise == ""):
        raise ValueError(f"{FRANCHISE_SEPARATOR!r} must join two non-empty parts, got {cell!r}")
    return cell


def _decode_genres(cell: str) -> list[str] | None:
    values = decode_list(cell)
    if values is not None:
        if "" in values:
            raise ValueError("genres must not contain empty strings")
        if len(set(values)) != len(values):
            raise ValueError(f"genres must not contain duplicates: {values!r}")
    return values


def _decode_specs(cell: str) -> list[str] | None:
    values = decode_list(cell)
    if values is None:
        return None
    core = values
    if "" in values:
        # Only the legacy [..., NOTICE, '', ''] tail may contain empty strings.
        if len(values) < 3 or values[-2:] != ["", ""] or values[-3] not in SPECS_NOTICES:
            raise ValueError(f"empty strings are only allowed as [NOTICE, '', ''] at the end: {values!r}")
        core = values[:-2]
    if "" in core:
        raise ValueError(f"unexpected empty string in specs: {values!r}")
    if len(set(core)) != len(core):
        raise ValueError(f"specs must not contain duplicates: {values!r}")
    return values


def _decode_tags(cell: str) -> list[str] | None:
    values = decode_list(cell)
    # Duplicates are legal: Steam lists 'Early Access' twice for 2,014 legacy rows.
    if values is not None and "" in values:
        raise ValueError("tags must not contain empty strings")
    return values


DECODERS = {
    "id": _decode_id,
    "title": _require_text,
    "genres": _decode_genres,
    "developer": _optional_text,
    "publisher": _decode_publisher,
    "release_date": decode_release_date,
    "app_name": _require_text,
    "specs": _decode_specs,
    "tags": _decode_tags,
    "price": _decode_price,
    "sentiment": decode_sentiment,
    "early_access": _decode_optional_bool,
    "metascore": decode_metascore,
}
assert tuple(DECODERS) == COLUMNS


# --------------------------------------------------------------------------
# Typed record
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GameRecord:
    """One row of the contract as typed Python values. ``None`` means an empty cell."""

    id: int
    title: str
    genres: tuple[str, ...] | None
    developer: str | None
    publisher: str | None
    release_date: str
    app_name: str
    specs: tuple[str, ...] | None
    tags: tuple[str, ...] | None
    price: int | None
    sentiment: str | None
    early_access: bool | None
    metascore: int | None

    @property
    def release_date_parsed(self) -> dt.date | None:
        return parse_release_date(self.release_date)

    def to_row(self) -> dict[str, str]:
        row = {
            "id": encode_uint(self.id),
            "title": self.title,
            "genres": encode_list(self.genres),
            "developer": self.developer or "",
            "publisher": self.publisher or "",
            "release_date": encode_release_date(self.release_date),
            "app_name": self.app_name,
            "specs": encode_list(self.specs),
            "tags": encode_list(self.tags),
            "price": "" if self.price is None else encode_uint(self.price),
            "sentiment": encode_sentiment(self.sentiment),
            "early_access": "" if self.early_access is None else encode_bool(self.early_access),
            "metascore": encode_metascore(self.metascore),
        }
        violations = validate_row(row)
        if violations:
            raise SchemaError(violations)
        return row

    @classmethod
    def from_row(cls, row: Mapping[str, str]) -> GameRecord:
        violations = validate_row(row)
        if violations:
            raise SchemaError(violations)
        values = {c: DECODERS[c](row[c]) for c in COLUMNS}
        for c in ("genres", "specs", "tags"):
            if values[c] is not None:
                values[c] = tuple(values[c])
        return cls(**values)


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    message: str
    row: int | None = None  # 1-based data-row number (header excluded)
    column: str | None = None

    def __str__(self) -> str:
        where = ", ".join(p for p in (f"row {self.row}" if self.row else "", self.column or "") if p)
        return f"{where}: {self.message}" if where else self.message


class SchemaError(ValueError):
    def __init__(self, violations: Sequence[Violation]):
        self.violations = list(violations)
        shown = "; ".join(map(str, self.violations[:5]))
        more = f" (+{len(self.violations) - 5} more)" if len(self.violations) > 5 else ""
        super().__init__(f"{len(self.violations)} schema violation(s): {shown}{more}")


def validate_header(header: Sequence[str]) -> list[Violation]:
    if tuple(header) != COLUMNS:
        return [Violation(f"header must be {','.join(COLUMNS)!r}, got {','.join(header)!r}")]
    return []


def validate_row(row: Mapping[str, str], row_number: int | None = None) -> list[Violation]:
    """Validate one row of raw cell strings keyed by column name."""
    if set(row) != set(COLUMNS):
        return [Violation(f"row must have exactly the columns {COLUMNS}, got {tuple(row)}", row_number)]
    out: list[Violation] = []
    decoded = {}
    for column in COLUMNS:
        cell = row[column]
        if not isinstance(cell, str):
            out.append(Violation(f"cell must be str, got {type(cell).__name__}", row_number, column))
            continue
        if "\r" in cell or "\n" in cell:
            out.append(Violation("cell must not contain line breaks", row_number, column))
            continue
        try:
            decoded[column] = DECODERS[column](cell)
        except ValueError as exc:
            out.append(Violation(str(exc), row_number, column))

    if "title" in decoded and "app_name" in decoded:
        if decoded["title"] != title_from_app_name(decoded["app_name"]):
            out.append(Violation("title must equal html.escape(app_name, quote=False)", row_number, "title"))
    if "early_access" in decoded and "genres" in decoded:
        in_genres = EARLY_ACCESS_GENRE in (decoded["genres"] or ())
        if decoded["early_access"] is None:
            # Blank means unverified, which is only possible when the genres evidence is missing.
            if decoded["genres"] is not None:
                out.append(Violation("early_access may be blank only when genres is blank", row_number, "early_access"))
        elif decoded["early_access"] != in_genres:
            out.append(
                Violation(f"early_access must be TRUE exactly when genres contains {EARLY_ACCESS_GENRE!r}",
                          row_number, "early_access")
            )
    return out


def validate_rows(rows: Iterable[Sequence[str]]) -> list[Violation]:
    """Validate a header row followed by data rows (as produced by ``csv.reader``)."""
    it = iter(rows)
    try:
        header = next(it)
    except StopIteration:
        return [Violation("file is empty; a header row is required")]
    out = validate_header(header)
    if out:
        return out
    seen_ids: dict[str, int] = {}
    for n, cells in enumerate(it, start=1):
        if len(cells) != len(COLUMNS):
            out.append(Violation(f"expected {len(COLUMNS)} fields, got {len(cells)}", n))
            continue
        row = dict(zip(COLUMNS, cells))
        out.extend(validate_row(row, n))
        if row["id"] in seen_ids:
            out.append(Violation(f"duplicate id (first seen in row {seen_ids[row['id']]})", n, "id"))
        else:
            seen_ids[row["id"]] = n
    return out


def validate_file(path: str | Path) -> list[Violation]:
    """Validate a CSV file's encoding, line endings, header, and every row. Read-only."""
    raw = Path(path).read_bytes()
    out: list[Violation] = []
    if raw.startswith(UTF8_BOM):
        out.append(Violation("file must not start with a UTF-8 BOM"))
    try:
        text = raw.decode(ENCODING)
    except UnicodeDecodeError as exc:
        return out + [Violation(f"file is not valid UTF-8: {exc}")]
    if text.count("\n") != text.count("\r\n") or text.count("\r") != text.count("\r\n"):
        out.append(Violation("all line terminators must be CRLF"))
    out.extend(validate_rows(csv.reader(io.StringIO(text.removeprefix("﻿"), newline=""))))
    return out


# --------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------


def read_rows(path: str | Path) -> Iterator[dict[str, str]]:
    """Yield raw rows as ``{column: cell}`` without any type coercion.

    (``pandas.read_csv`` defaults would turn publishers named 'None' or 'N/A'
    into NaN; use ``dtype=str, keep_default_na=False`` if pandas is needed.)
    """
    with open(path, newline="", encoding=ENCODING) as f:
        reader = csv.reader(f)
        header = next(reader, None)
        violations = validate_header(header or [])
        if violations:
            raise SchemaError(violations)
        for cells in reader:
            yield dict(zip(COLUMNS, cells, strict=True))


def read_records(path: str | Path) -> Iterator[GameRecord]:
    for row in read_rows(path):
        yield GameRecord.from_row(row)


def write_rows(path: str | Path, rows: Iterable[GameRecord | Mapping[str, str]], *, overwrite: bool = False) -> int:
    """Write rows in the legacy serialization and return the number of data rows.

    Rows are validated before anything is written. Existing files are never
    replaced unless ``overwrite=True``. Unlike the legacy file, every record
    (including the last) is followed by CRLF.
    """
    encoded: list[dict[str, str]] = []
    violations: list[Violation] = []
    seen_ids: set[str] = set()
    for n, row in enumerate(rows, start=1):
        row = row.to_row() if isinstance(row, GameRecord) else dict(row)
        violations.extend(validate_row(row, n))
        if row.get("id") in seen_ids:
            violations.append(Violation("duplicate id", n, "id"))
        seen_ids.add(row.get("id"))
        encoded.append(row)
    if violations:
        raise SchemaError(violations)

    with open(path, "w" if overwrite else "x", newline="", encoding=ENCODING) as f:
        writer = csv.writer(f, lineterminator=LINE_TERMINATOR, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(COLUMNS)
        writer.writerows([row[c] for c in COLUMNS] for row in encoded)
    return len(encoded)

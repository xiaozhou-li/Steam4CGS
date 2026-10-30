"""Check the schema contract against the original steamproducts2020.csv (read-only)."""

import collections
import csv
import hashlib

import pytest

from crawler import schema

LEGACY_SHA256 = "3a7b7bd5b59565440d05c543557607b498c45590ebddc3f3e7a77afe9d095be8"
LEGACY_ROWS = 23034


@pytest.fixture(scope="module")
def legacy_bytes(legacy_csv) -> bytes:
    return legacy_csv.read_bytes()


@pytest.fixture(scope="module")
def legacy_rows(legacy_csv) -> list[dict[str, str]]:
    return list(schema.read_rows(legacy_csv))


def test_legacy_file_is_unmodified(legacy_bytes):
    assert hashlib.sha256(legacy_bytes).hexdigest() == LEGACY_SHA256


def test_legacy_file_passes_full_validation(legacy_csv):
    violations = schema.validate_file(legacy_csv)
    assert violations == [], "\n".join(map(str, violations[:20]))


def test_legacy_row_count_and_header(legacy_csv, legacy_rows):
    with open(legacy_csv, newline="", encoding="utf-8") as f:
        assert tuple(next(csv.reader(f))) == schema.COLUMNS
    assert len(legacy_rows) == LEGACY_ROWS


def test_every_legacy_row_roundtrips_through_record(legacy_rows):
    for n, row in enumerate(legacy_rows, start=1):
        assert schema.GameRecord.from_row(row).to_row() == row, f"row {n}"


def test_writer_reproduces_legacy_bytes(legacy_rows, legacy_bytes, tmp_path):
    out = tmp_path / "rewritten.csv"
    schema.write_rows(out, legacy_rows)
    # The only difference: the legacy file has no CRLF after its final record.
    assert out.read_bytes() == legacy_bytes + b"\r\n"


def test_legacy_null_counts(legacy_rows):
    empty = {c: sum(r[c] == "" for r in legacy_rows) for c in schema.COLUMNS}
    assert empty == {
        "id": 0, "title": 0, "genres": 37, "developer": 0, "publisher": 5, "release_date": 0,
        "app_name": 0, "specs": 0, "tags": 11, "price": 0, "sentiment": 1722,
        "early_access": 0, "metascore": 20665,
    }


def test_legacy_value_distributions(legacy_rows):
    records = [schema.GameRecord.from_row(r) for r in legacy_rows]
    assert collections.Counter(r.early_access for r in records) == {False: 20985, True: 2049}
    assert min(r.price for r in records) == 0 and max(r.price for r in records) == 642
    scores = [r.metascore for r in records if r.metascore is not None]
    assert (min(scores), max(scores)) == (20, 96)
    assert sum(r.release_date_parsed is not None for r in records) == 22964
    assert sum(r.publisher is not None and schema.FRANCHISE_SEPARATOR in r.publisher for r in records) == 3191
    assert max(len(r.tags) for r in records if r.tags) == 20
    assert all(r.specs[0] == "Single-player" for r in records)

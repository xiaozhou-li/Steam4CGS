"""Unit tests for the schema contract using synthetic rows."""

import datetime as dt
import socket

import pytest

from crawler import schema
from crawler.schema import COLUMNS, GameRecord, SchemaError, validate_row


def make_row(**overrides) -> dict[str, str]:
    row = {
        "id": "976730",
        "title": "Sword &amp; Shield",
        "genres": "['Action', 'Early Access']",
        "developer": "Studio A, Studio B",
        "publisher": "Big PubFranchise:Big Series",
        "release_date": "3 Dec, 2019",
        "app_name": "Sword & Shield",
        "specs": "['Single-player', 'Profile Features Limited\\xa0', '', '']",
        "tags": "['Early Access', \"Beat 'em up\", 'Early Access']",
        "price": "40",
        "sentiment": "Very Positive",
        "early_access": "TRUE",
        "metascore": "",
    }
    row.update(overrides)
    return row


def errors(row) -> list[tuple[str | None, str]]:
    return [(v.column, v.message) for v in validate_row(row)]


def test_column_contract_is_exact():
    assert COLUMNS == (
        "id", "title", "genres", "developer", "publisher", "release_date", "app_name",
        "specs", "tags", "price", "sentiment", "early_access", "metascore",
    )
    assert schema.REQUIRED_COLUMNS | schema.NULLABLE_COLUMNS == set(COLUMNS)
    assert not schema.REQUIRED_COLUMNS & schema.NULLABLE_COLUMNS


def test_valid_row_has_no_violations():
    assert errors(make_row()) == []


def test_row_with_missing_or_extra_columns_is_rejected():
    row = make_row()
    del row["metascore"]
    assert errors(row)
    assert errors({**make_row(), "extra": ""})


@pytest.mark.parametrize("column", sorted(schema.REQUIRED_COLUMNS))
def test_required_columns_reject_empty(column):
    assert any(c == column for c, _ in errors(make_row(**{column: ""})))


@pytest.mark.parametrize("column", sorted(schema.NULLABLE_COLUMNS))
def test_nullable_columns_accept_empty(column):
    row = make_row(**{column: ""})
    if column == "genres":
        row["early_access"] = "FALSE"  # keep the cross-field rule satisfied
    if column == "early_access":
        row["genres"] = ""  # unverified early access requires missing genres
    assert errors(row) == []


def test_new_data_nullable_columns_round_trip_as_none():
    row = make_row(developer="", specs="", price="", early_access="", genres="")
    record = GameRecord.from_row(row)
    assert (record.developer, record.specs, record.price, record.early_access) == (None, None, None, None)
    assert record.to_row() == row


def test_blank_early_access_means_unknown_and_is_allowed_with_genres():
    assert errors(make_row(early_access="")) == []


def test_known_early_access_must_agree_with_present_genres():
    assert errors(make_row(genres="['Action']", early_access="TRUE"))
    assert errors(make_row(genres="['Action', 'Early Access']", early_access="FALSE"))
    assert errors(make_row(genres="", early_access="TRUE")) == []  # no genres: nothing to contradict


def test_cells_must_not_contain_line_breaks():
    assert errors(make_row(developer="A\nB"))
    assert errors(make_row(developer="A\r\nB"))


# --- id / price / metascore -------------------------------------------------

@pytest.mark.parametrize("cell", ["0", "-1", "01", "1.0", " 5", "abc", "1e3"])
def test_id_rejects_non_canonical(cell):
    assert errors(make_row(id=cell))


@pytest.mark.parametrize("cell", ["0", "1", "642"])
def test_price_accepts_whole_units(cell):
    assert errors(make_row(price=cell)) == []


@pytest.mark.parametrize("cell", ["39.99", "$40", "040", "-1", "40.0"])
def test_price_rejects_non_integer(cell):
    assert errors(make_row(price=cell))


@pytest.mark.parametrize(("cents", "expected"), [(0, 0), (49, 0), (50, 1), (99, 1), (3999, 40), (2950, 30), (64199, 642)])
def test_price_from_cents_rounds_half_up(cents, expected):
    assert schema.price_from_cents(cents) == expected


@pytest.mark.parametrize("bad", [-1, 39.99, True, "3999"])
def test_price_from_cents_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        schema.price_from_cents(bad)


@pytest.mark.parametrize("cell", ["0", "20", "96", "100"])
def test_metascore_accepts_range(cell):
    assert schema.decode_metascore(cell) == int(cell)


@pytest.mark.parametrize("cell", ["101", "77.0", "nan", "NaN", "077"])
def test_metascore_rejects_invalid(cell):
    with pytest.raises(ValueError):
        schema.decode_metascore(cell)


def test_metascore_empty_is_none():
    assert schema.decode_metascore("") is None
    assert schema.encode_metascore(None) == ""


# --- booleans -----------------------------------------------------------------

@pytest.mark.parametrize("cell", ["true", "True", "1", "0", "False", "yes", ""])
def test_bool_only_uppercase_literals(cell):
    with pytest.raises(ValueError):
        schema.decode_bool(cell)


def test_bool_roundtrip():
    assert schema.encode_bool(True) == "TRUE" and schema.decode_bool("TRUE") is True
    assert schema.encode_bool(False) == "FALSE" and schema.decode_bool("FALSE") is False
    with pytest.raises(ValueError):
        schema.encode_bool(1)


def test_early_access_must_match_genres():
    assert [c for c, _ in errors(make_row(early_access="FALSE"))] == ["early_access"]
    assert errors(make_row(genres="['Action']", early_access="TRUE"))
    assert errors(make_row(genres="['Action']", early_access="FALSE")) == []


# --- lists ------------------------------------------------------------------------

def test_list_roundtrip_uses_python_repr():
    values = ["Action", "Beat 'em up", "Profile Features Limited\xa0"]
    cell = schema.encode_list(values)
    assert cell == "['Action', \"Beat 'em up\", 'Profile Features Limited\\xa0']"
    assert schema.decode_list(cell) == values


def test_empty_list_encodes_as_empty_cell():
    assert schema.encode_list([]) == ""
    assert schema.encode_list(None) == ""
    assert schema.decode_list("") is None
    with pytest.raises(ValueError):
        schema.decode_list("[]")


@pytest.mark.parametrize(
    "cell",
    [
        '["Action"]',            # double quotes are not canonical repr
        "['Action','Indie']",    # missing space after comma
        "Action, Indie",         # not a list literal
        "('Action',)",           # tuple
        "[1, 2]",                # non-str elements
        "['Action'",             # unparseable
        "[__import__('os')]",    # never evaluated
    ],
)
def test_list_rejects_non_canonical(cell):
    with pytest.raises(ValueError):
        schema.decode_list(cell)


def test_encode_list_rejects_plain_string():
    with pytest.raises(ValueError):
        schema.encode_list("Action")


def test_genres_reject_duplicates_and_empty_strings():
    assert errors(make_row(genres="['Action', 'Action', 'Early Access']"))
    assert errors(make_row(genres="['Action', '', 'Early Access']"))


def test_tags_allow_duplicates_but_not_empty_strings():
    assert errors(make_row(tags="['Early Access', 'Early Access']")) == []
    assert errors(make_row(tags="['Indie', '']"))


@pytest.mark.parametrize(
    "cell",
    [
        "['Single-player']",
        "['Single-player', 'Steam is learning about this game\\xa0', '', '']",
        "['Single-player', 'Profile Features Limited\\xa0']",
    ],
)
def test_specs_accepts_legacy_shapes(cell):
    assert errors(make_row(specs=cell)) == []


@pytest.mark.parametrize(
    "cell",
    [
        "['Single-player', '', '']",                                   # no notice before the blanks
        "['Single-player', 'Profile Features Limited\\xa0', '']",      # only one blank
        "['', 'Profile Features Limited\\xa0', '', '']",               # blank outside the tail
        "['Single-player', 'Single-player']",                          # duplicate
    ],
)
def test_specs_rejects_other_empty_or_duplicate_elements(cell):
    assert errors(make_row(specs=cell))


# --- release dates ------------------------------------------------------------------

@pytest.mark.parametrize(
    ("date", "cell"),
    [(dt.date(2019, 12, 3), "3 Dec, 2019"), (dt.date(2015, 3, 10), "10 Mar, 2015"), (dt.date(2020, 5, 31), "31 May, 2020")],
)
def test_release_date_format_and_parse(date, cell):
    assert schema.encode_release_date(date) == cell
    assert schema.parse_release_date(cell) == date


@pytest.mark.parametrize("cell", ["TBA", "Q1 2020", "Oct 2010", "2020", "When it's done", "2019年第四季度", "Early 2020, Your Grace"])
def test_release_date_free_text_is_allowed_but_unparsed(cell):
    assert errors(make_row(release_date=cell)) == []
    assert schema.parse_release_date(cell) is None


@pytest.mark.parametrize("cell", ["03 Dec, 2019", "3 December, 2019", "31 Feb, 2019", "3 dec, 2019", " 3 Dec, 2019", ""])
def test_release_date_rejects_malformed_dates(cell):
    assert errors(make_row(release_date=cell))


# --- sentiment --------------------------------------------------------------------

@pytest.mark.parametrize("cell", list(schema.SENTIMENT_LABELS) + [f"{n} user reviews" for n in range(1, 10)])
def test_sentiment_accepts_known_values(cell):
    assert schema.decode_sentiment(cell) == cell


@pytest.mark.parametrize("cell", ["very positive", "10 user reviews", "0 user reviews", "1 user review", "No user reviews", "Positive "])
def test_sentiment_rejects_unknown(cell):
    with pytest.raises(ValueError):
        schema.decode_sentiment(cell)


def test_sentiment_empty_is_none():
    assert schema.decode_sentiment("") is None
    assert schema.encode_sentiment(None) == ""


# --- text fields --------------------------------------------------------------------

def test_title_is_html_escaped_app_name():
    assert schema.title_from_app_name("A & B <C>") == "A &amp; B &lt;C&gt;"
    assert errors(make_row(title="Sword & Shield"))  # unescaped title
    assert errors(make_row(title="Sword &amp; Shield", app_name="Sword &amp; Shield"))
    assert errors(make_row(title="It's", app_name="It's")) == []  # quotes are not escaped


def test_publisher_franchise_split_and_join():
    assert schema.split_publisher("Paradox InteractiveFranchise:Cities: Skylines") == ("Paradox Interactive", "Cities: Skylines")
    assert schema.split_publisher("Ubisoft") == ("Ubisoft", None)
    assert schema.join_publisher("Paradox Interactive", "Cities: Skylines") == "Paradox InteractiveFranchise:Cities: Skylines"
    assert schema.join_publisher("Ubisoft") == "Ubisoft"


@pytest.mark.parametrize("cell", ["Franchise:X", "PubFranchise:", "AFranchise:BFranchise:C"])
def test_publisher_rejects_malformed_franchise(cell):
    assert errors(make_row(publisher=cell))


@pytest.mark.parametrize("cell", ["None", "N/A", "n/a", " Leading space", "Trailing space "])
def test_publisher_literal_text_is_preserved(cell):
    assert errors(make_row(publisher=cell)) == []
    assert GameRecord.from_row(make_row(publisher=cell)).publisher == cell


# --- record + file I/O --------------------------------------------------------------

def test_record_roundtrip():
    row = make_row()
    record = GameRecord.from_row(row)
    assert record.id == 976730
    assert record.genres == ("Action", "Early Access")
    assert record.price == 40 and record.early_access is True and record.metascore is None
    assert record.release_date_parsed == dt.date(2019, 12, 3)
    assert record.to_row() == row


def test_record_from_invalid_row_raises():
    with pytest.raises(SchemaError):
        GameRecord.from_row(make_row(price="39.99"))


def test_write_then_validate_file(tmp_path):
    path = tmp_path / "out.csv"
    second = make_row(id="1", genres="['Indie']", early_access="FALSE", sentiment="", metascore="77")
    assert schema.write_rows(path, [GameRecord.from_row(make_row()), second]) == 2
    raw = path.read_bytes()
    assert raw.startswith(b"id,title,genres,developer,publisher,release_date,app_name,specs,tags,price,sentiment,early_access,metascore\r\n")
    assert raw.count(b"\r\n") == raw.count(b"\n") == 3
    assert schema.validate_file(path) == []
    assert [r.id for r in schema.read_records(path)] == [976730, 1]


def test_write_refuses_to_overwrite(tmp_path):
    path = tmp_path / "out.csv"
    path.write_text("keep me")
    with pytest.raises(FileExistsError):
        schema.write_rows(path, [make_row()])
    assert path.read_text() == "keep me"


def test_write_validates_before_touching_disk(tmp_path):
    path = tmp_path / "out.csv"
    with pytest.raises(SchemaError):
        schema.write_rows(path, [make_row(), make_row()])  # duplicate id
    assert not path.exists()


def test_validate_file_detects_transport_problems(tmp_path):
    good = tmp_path / "good.csv"
    schema.write_rows(good, [make_row()])
    body = good.read_bytes()

    bom = tmp_path / "bom.csv"
    bom.write_bytes(schema.UTF8_BOM + body)
    assert any("BOM" in v.message for v in schema.validate_file(bom))

    lf = tmp_path / "lf.csv"
    lf.write_bytes(body.replace(b"\r\n", b"\n"))
    assert any("CRLF" in v.message for v in schema.validate_file(lf))

    latin = tmp_path / "latin.csv"
    latin.write_bytes(body.replace(b"Sword", "Sw\xf6rd".encode("latin-1")))
    assert any("UTF-8" in v.message for v in schema.validate_file(latin))

    header = tmp_path / "header.csv"
    header.write_bytes(body.replace(b"app_name", b"name", 1))
    assert any("header" in v.message for v in schema.validate_file(header))


def test_network_is_blocked():
    with pytest.raises(RuntimeError, match="network access is disabled"):
        socket.create_connection(("store.steampowered.com", 443))

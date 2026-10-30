"""Field-level output checks: a clean run passes, and every class of defect is caught."""

from __future__ import annotations

import shutil

import pytest
from fakes import FAKE_KEY, make_transport
from sample_data import APPDETAILS, SampleSteam

from crawler import cli, pipeline, quality, schema
from crawler.transport import RetryPolicy

CATALOG = {a: 1_700_000_000 for a in APPDETAILS}


def run(base, legacy_csv, adapter=None, *extra):
    adapter = adapter or SampleSteam(catalog=CATALOG)
    argv = ["run", "--base-dir", str(base), "--work-dir", str(base / "work"), "--legacy-csv", str(legacy_csv),
            "--end-date", "2026-10-30", *extra]
    return cli.main(argv, environ={"STEAM_API_KEY": FAKE_KEY},
                    transport=make_transport(adapter, retry=RetryPolicy(max_attempts=1)))


@pytest.fixture(scope="module")
def clean(tmp_path_factory, legacy_csv):
    base = tmp_path_factory.mktemp("clean")
    assert run(base, legacy_csv) == 0
    return base


@pytest.fixture
def ws(clean, tmp_path, legacy_csv):
    copy = tmp_path / "ws"
    shutil.copytree(clean, copy)
    return pipeline.Workspace.at(copy, copy / "work", legacy_csv)


def results(ws):
    return {c.name: c for c in quality.check_outputs(ws)}


def failed(ws):
    return {name for name, c in results(ws).items() if not c.ok}


def edit(path, old, new, count=1):
    data = path.read_bytes()
    assert old in data, old
    path.write_bytes(data.replace(old, new, count))


def test_clean_outputs_pass_every_check(ws):
    checks = results(ws)
    assert len(checks) == 12
    assert all(c.ok for c in checks.values()), {n: c.failures for n, c in checks.items() if not c.ok}


@pytest.mark.parametrize(
    ("old", "new", "check"),
    [
        (b"metascore\r\n", b"metacritic\r\n", "exact 13-column schema"),
        (b",metascore\r\n", b",metascore,extra\r\n", "exact 13-column schema"),
        (b"\r\n", b"\n", "CSV serialization"),
        (b"9900002,Sample Sandbox,", b'9900002,"Sample Sandbox",', "CSV serialization"),       # needless quoting
        (b"['Single-player', 'Online Co-op', 'Co-op']", b'["Single-player"]', "CSV serialization"),  # non-canonical
        (b"\r\n9900002,", b"\r\n9900001,", "unique AppIDs"),
        (b"\r\n9900002,", b"\r\n976730,", "unique AppIDs"),
        (b'"2 Feb, 2024"', b'"2 Feb, 2019"', "release dates within the window"),
        (b'"2 Feb, 2024"', b'"2 Feb, 2027"', "release dates within the window"),
        (b"Sample Racer,\"['Single-player', 'Steam Achievements']\",,10,", b"Sample Racer,\"['Single-player', 'Steam Achievements']\",,9,",
         "integer USD prices from the regular price"),
        (b",20,Very Positive,", b",15,Very Positive,", "integer USD prices from the regular price"),  # sale price
        (b",20,Very Positive,", b",19.99,Very Positive,", "CSV serialization"),
        (b"\"['Action', 'Adventure', 'Indie']\"", b"\"['Indie', 'Action', 'Adventure']\"", "list formatting"),
        (b",Very Positive,FALSE,", b",Very Positive,false,", "CSV serialization"),
        (b",Very Positive,FALSE,", b",Very Positive,,", "early_access booleans"),
        (b"Sample Studio, Port House (Mac)", b"", "missing-data handling"),
        (b",Very Positive,", b",Mixed,", "missing-data handling"),
        (b",FALSE,84\r\n", b",FALSE,101\r\n", "metascore integers 0-100"),
        (b"\r\n9900002,", b"\r\nabc,", "CSV serialization"),                       # non-numeric id
    ],
)
def test_defects_in_the_new_csv_are_caught(ws, old, new, check):
    edit(ws.new_csv, old, new, count=-1 if old == b"\r\n" else 1)
    assert check in failed(ws)


def test_merged_tampering_is_caught(ws):
    edit(ws.merged_csv, b"Halo: The Master Chief Collection", b"Halo: The Master Chief Collectio")
    assert "merged = original bytes + new rows" in failed(ws)


def test_bom_is_caught(ws):
    ws.new_csv.write_bytes(schema.UTF8_BOM + ws.new_csv.read_bytes())
    assert "CSV serialization" in failed(ws)


def test_sentiment_without_verified_review_is_caught(ws):
    path = ws.reviews_out / "review_summaries.csv"
    edit(path, b"9900001,ok,", b"9900001,invalid_response,")
    assert "missing-data handling" in failed(ws)


def test_false_requires_explicit_flag(ws):
    path = ws.metadata_out / "early_access.csv"
    edit(path, b"9900001,FALSE,false,store item is_early_access=false,false,",
         b"9900001,FALSE,false,store item is_early_access=false,absent,")
    assert "early_access booleans" in failed(ws)


def test_report_renders_every_section(ws):
    text = quality.render_report(ws, quality.check_outputs(ws), tests="10 passed")
    for heading in ("## Validation checks", "## Collection coverage", "## Missingness per field", "## Exclusions",
                    "## API failures", "## Source-specific limitations", "## Test suite", "## Reproduce"):
        assert heading in text
    assert "| `sentiment` | 2 | 2 | 50.0 |" in text  # 2 verified labels, 2 blanks (no_reviews + invalid)
    assert "**FAIL**" not in text


def test_quality_command_writes_report(ws, capsys):
    argv = ["quality", "--base-dir", str(ws.base), "--work-dir", str(ws.work), "--legacy-csv", str(ws.legacy_csv)]
    assert cli.main(argv, environ={}) == 0
    assert (ws.base / "reports" / "quality_report.md").exists()
    edit(ws.new_csv, b"\r\n9900002,", b"\r\n9900001,")
    assert cli.main(argv, environ={}) == cli.EXIT_ERROR
    assert "[FAIL] unique AppIDs" in capsys.readouterr().out

"""Back-test machinery (offline): sampling, isolation from the outputs, and structural comparison."""

from __future__ import annotations

import csv
import json

import pytest
from fakes import make_transport
from sample_data import SampleSteam

from crawler import backtest, cli, pipeline, schema
from crawler.transport import RetryPolicy

HALO = {
    "type": "game", "name": "Halo: The Master Chief Collection", "steam_appid": 976730, "is_free": False,
    "developers": ["343 Industries", "Splash Damage", "Ruffian Games", "Bungie"],
    "publishers": ["Xbox Game Studios"], "genres": [{"id": "1", "description": "Action"}],
    "categories": [{"id": 2, "description": "Single-player"}, {"id": 1, "description": "Multi-player"},
                   {"id": 22, "description": "Steam Achievements"}],
    "price_overview": {"currency": "USD", "initial": 3999, "final": 3999, "discount_percent": 0},
    "release_date": {"coming_soon": False, "date": "Dec 3, 2019"}, "metacritic": {"score": 85},
}


def ctx(tmp_path, legacy_csv, adapter):
    ws = pipeline.Workspace.at(tmp_path, tmp_path / "work", legacy_csv)
    return pipeline.Context(ws, {}, transport=make_transport(adapter, retry=RetryPolicy(max_attempts=1)),
                            log=lambda _: None)


def test_sample_is_deterministic_stratified_and_from_the_original(legacy_csv):
    a = backtest.select_sample(legacy_csv, 25, 2020)
    assert a == backtest.select_sample(legacy_csv, 25, 2020)
    assert a != backtest.select_sample(legacy_csv, 25, 7)
    assert len(a) == 25 and len({x for x, _ in a}) == 25
    legacy = {int(r["id"]): r for r in schema.read_rows(legacy_csv)}
    assert all(x in legacy for x, _ in a)
    strata = {s for _, s in a}
    assert set(backtest.STRATA) <= strata
    for appid, stratum in a:
        assert backtest.STRATA[stratum](legacy[appid])


@pytest.mark.parametrize(
    ("column", "old", "new", "compatible", "equal", "note"),
    [
        ("genres", "['Action']", "['Action', 'Indie']", True, False, ""),
        ("genres", "['Action']", '["Action"]', False, False, "current cell invalid"),
        ("price", "40", "40", True, True, ""),
        ("price", "40", "39.99", False, False, "current cell invalid"),
        ("release_date", "3 Dec, 2019", "3 Dec, 2019", True, True, "same date"),
        ("release_date", "Coming soon", "3 Dec, 2021", True, False, "2020 free text"),
        ("release_date", "3 Dec, 2019", "5 Dec, 2019", True, False, "date differs"),
        ("early_access", "FALSE", "", True, False, ""),
        ("early_access", "FALSE", "false", False, False, "current cell invalid"),
        ("sentiment", "Very Positive", "", True, False, ""),
        ("metascore", "", "77", True, False, ""),
    ],
)
def test_compare_cell(column, old, new, compatible, equal, note):
    ok, same, why = backtest.compare_cell(column, old, new)
    assert (ok, same) == (compatible, equal) and note in why


def test_backtest_extracts_original_games_in_isolation(tmp_path, legacy_csv):
    adapter = SampleSteam(appdetails={976730: HALO, 710920: None})
    summary = backtest.run_backtest(ctx(tmp_path, legacy_csv, adapter), [(976730, "explicit"), (710920, "explicit")])
    assert (summary["extracted"], summary["unavailable"], summary["rows_valid"], summary["rows_serializable"]) == (1, 1, 1, 1)
    assert summary["unavailable_reasons"] == ["710920: store_details_unavailable"]
    cols = summary["columns"]
    assert all(cols[c]["compatible"] == 1 for c in schema.COLUMNS)
    assert cols["id"]["equal"] == cols["release_date"]["equal"] == cols["price"]["equal"] == 1
    assert "2020 'Franchise:' suffixes" in cols["publisher"]["note"]
    with open(tmp_path / "reports" / "backtest_comparison.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 13 and tuple(rows[0]) == backtest.COMPARISON_FIELDS
    assert json.loads((tmp_path / "reports" / "backtest_summary.json").read_text())["sample"] == 2
    # Isolation: nothing reaches the pipeline's state or the final outputs.
    ws = pipeline.Workspace.at(tmp_path, tmp_path / "work", legacy_csv)
    assert not ws.metadata_state.exists() and not ws.new_csv.exists() and not ws.merged_csv.exists()
    assert (tmp_path / "work" / "backtest" / "metadata.sqlite3").exists()


def test_cli_backtest_guards(tmp_path, legacy_csv, capsys):
    base = ["--base-dir", str(tmp_path), "--work-dir", str(tmp_path / "work"), "--legacy-csv", str(legacy_csv)]
    assert cli.main(["backtest", *base, "--dry-run", "--sample", "6"], environ={}) == 0
    assert "would back-test 6 original game(s)" in capsys.readouterr().out
    assert cli.main(["backtest", *base, "--appid", "9900001"], environ={}) == cli.EXIT_USAGE
    assert "not in the original CSV" in capsys.readouterr().out
    assert not (tmp_path / "work").exists()

"""End-to-end tests of the integrated crawler against an emulated Steam (no network)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil

import pytest
from fakes import FAKE_KEY, make_transport
from sample_data import APPDETAILS, SampleSteam

from crawler import cli, pipeline, schema
from crawler.transport import RetryPolicy

KEY_ENV = {"STEAM_API_KEY": FAKE_KEY}
LEGACY_IN_CATALOG = 976730  # Halo MCC: in steamproducts2020.csv
CATALOG = {**{a: 1_700_000_000 for a in APPDETAILS}, 9900006: 1_700_000_000, LEGACY_IN_CATALOG: 1_700_000_000}
ELIGIBLE = [9900001, 9900002, 9900003, 9900004]


def steam(**kwargs):
    appdetails = {**APPDETAILS, 9900006: 500}
    return SampleSteam(appdetails=appdetails, catalog=CATALOG, **kwargs)


def transport(adapter):
    return make_transport(adapter, retry=RetryPolicy(max_attempts=1))


def ws_args(tmp_path, legacy_csv):
    return ["--base-dir", str(tmp_path), "--work-dir", str(tmp_path / "work"), "--legacy-csv", str(legacy_csv)]


def cmd(tmp_path, legacy_csv, adapter, *argv, environ=KEY_ENV):
    command, *rest = argv
    return cli.main([command, *ws_args(tmp_path, legacy_csv), *rest], environ=environ, transport=transport(adapter))


def rows(path):
    return list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8"), newline="")))


def calls(adapter, path):
    return [q for p, q in adapter.calls if p == path]


@pytest.fixture(scope="module")
def base_run(tmp_path_factory, legacy_csv):
    """One full run shared by the module; tests that modify files work on a copy (``ran``)."""
    base = tmp_path_factory.mktemp("base")
    adapter = steam()
    code = cmd(base, legacy_csv, adapter, "run", "--end-date", "2026-10-30")
    return base, adapter, code


@pytest.fixture
def ran(base_run, tmp_path, legacy_csv):
    base, _, code = base_run
    copy = tmp_path / "ws"
    shutil.copytree(base, copy)
    return copy, legacy_csv, steam(), code


# --- full run -----------------------------------------------------------------------------------

def test_run_produces_all_outputs(ran, capsys):
    tmp_path, legacy_csv, adapter, code = ran
    assert code == 0
    for name in (pipeline.NEW_CSV, pipeline.MERGED_CSV, pipeline.PROVENANCE_CSV, pipeline.MANIFEST_JSON):
        assert (tmp_path / name).exists()
    assert (tmp_path / "work" / pipeline.FAILED_CSV).exists()
    assert cmd(tmp_path, legacy_csv, adapter, "validate") == 0
    assert "[ OK ] all output checks passed" in capsys.readouterr().out


def test_new_csv_has_exactly_the_legacy_columns_and_only_new_games(ran):
    tmp_path, legacy_csv, _, _ = ran
    new_bytes = (tmp_path / pipeline.NEW_CSV).read_bytes()
    assert new_bytes.split(b"\r\n", 1)[0] == legacy_csv.read_bytes().split(b"\r\n", 1)[0]
    data = rows(tmp_path / pipeline.NEW_CSV)
    assert tuple(data[0]) == schema.COLUMNS
    assert [int(r[0]) for r in data[1:]] == ELIGIBLE
    assert all(len(r) == 13 for r in data)
    assert schema.validate_file(tmp_path / pipeline.NEW_CSV) == []
    legacy_ids = {int(r[0]) for r in rows(legacy_csv)[1:]}
    assert not legacy_ids & {int(r[0]) for r in data[1:]}


def test_merged_csv_preserves_legacy_bytes_and_appends_new_rows(ran):
    tmp_path, legacy_csv, _, _ = ran
    legacy_bytes = legacy_csv.read_bytes()
    merged = (tmp_path / pipeline.MERGED_CSV).read_bytes()
    assert merged.startswith(legacy_bytes)
    merged_rows, legacy_rows, new_rows = rows(tmp_path / pipeline.MERGED_CSV), rows(legacy_csv), rows(tmp_path / pipeline.NEW_CSV)
    assert merged_rows[:len(legacy_rows)] == legacy_rows
    assert merged_rows[len(legacy_rows):] == new_rows[1:]
    ids = [r[0] for r in merged_rows[1:]]
    assert len(ids) == len(set(ids)) == pipeline.LEGACY_ROWS + len(ELIGIBLE)
    assert hashlib.sha256(legacy_bytes).hexdigest() == pipeline.LEGACY_SHA256  # original untouched


def test_provenance_manifest_and_failed_ids(ran):
    tmp_path, _, _, _ = ran
    with open(tmp_path / pipeline.PROVENANCE_CSV, newline="", encoding="utf-8") as f:
        prov = list(csv.DictReader(f))
    assert len(prov) == 13 * len(ELIGIBLE) and {int(p["appid"]) for p in prov} == set(ELIGIBLE)

    manifest = json.loads((tmp_path / pipeline.MANIFEST_JSON).read_text())
    assert manifest["window"] == {"start": "2020-01-01", "end": "2026-10-30"}
    assert manifest["crawl"]["new_games"] == 4
    assert manifest["legacy"]["sha256"] == pipeline.LEGACY_SHA256
    assert manifest["discovery"]["counts"] == {"eligible": 4, "excluded": 2, "failed": 1}
    assert manifest["reviews"]["sentiment_filled"] == 2
    for rel, info in manifest["outputs"].items():
        assert hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest() == info["sha256"]
    assert set(manifest["stages"]) == {"discover", "crawl", "export"}

    with open(tmp_path / "work" / pipeline.FAILED_CSV, newline="", encoding="utf-8") as f:
        failed = list(csv.DictReader(f))
    assert [(r["appid"], r["stage"]) for r in failed] == [("9900006", "discover")]
    assert "HTTP 500" in failed[0]["detail"]

    status = json.loads((tmp_path / "work" / "crawl_status.json").read_text())
    assert {k: v["state"] for k, v in status["stages"].items()} == {
        "discover": "finished", "crawl": "finished", "export": "finished", "validate": "finished"}


def test_appdetails_are_reused_from_cache_and_legacy_never_fetched(base_run):
    _, adapter, _ = base_run
    fetched = [int(q["appids"]) for q in calls(adapter, "/api/appdetails")]
    assert LEGACY_IN_CATALOG not in fetched
    # One network fetch per candidate (during discovery); the crawl reused the cache.
    assert sorted(fetched) == sorted([*APPDETAILS, 9900006])


def test_refresh_bypasses_the_cache(ran):
    tmp_path, legacy_csv, adapter, _ = ran
    adapter.calls.clear()
    assert cmd(tmp_path, legacy_csv, adapter, "crawl", "--refresh") == 0
    assert sorted(int(q["appids"]) for q in calls(adapter, "/api/appdetails")) == ELIGIBLE


def test_rerunning_is_idempotent(ran):
    tmp_path, legacy_csv, adapter, _ = ran
    before = {n: (tmp_path / n).read_bytes() for n in (pipeline.NEW_CSV, pipeline.MERGED_CSV)}
    prov_before = (tmp_path / pipeline.PROVENANCE_CSV).read_text().splitlines()
    assert cmd(tmp_path, legacy_csv, adapter, "run", "--end-date", "2026-10-30") == 0
    # Facts and complete rows are reused; only the failed discovery lookup (9900006) and the
    # partial row (9900004, whose tags request failed) are retried.
    assert [q["appids"] for q in calls(adapter, "/api/appdetails")] == ["9900006"]
    assert {n: (tmp_path / n).read_bytes() for n in before} == before
    prov_after = (tmp_path / pipeline.PROVENANCE_CSV).read_text().splitlines()
    assert [l for l in prov_after if not l.startswith("9900004,")] == \
           [l for l in prov_before if not l.startswith("9900004,")]


# --- dry run, limit, resume ----------------------------------------------------------------------------

def test_dry_run_on_fresh_workspace_makes_no_requests_or_files(tmp_path, legacy_csv, capsys):
    adapter = steam()
    assert cmd(tmp_path, legacy_csv, adapter, "run", "--dry-run") == 0
    assert adapter.calls == [] and not (tmp_path / "work").exists() and not (tmp_path / pipeline.NEW_CSV).exists()
    assert "dry run" in capsys.readouterr().out


def test_dry_run_after_a_run_changes_nothing(ran, capsys):
    tmp_path, legacy_csv, adapter, _ = ran
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and "sqlite3" not in p.name}
    adapter.calls.clear()
    for argv in (("run", "--dry-run"), ("crawl", "--dry-run"), ("export", "--dry-run"), ("discover", "--dry-run")):
        assert cmd(tmp_path, legacy_csv, adapter, *argv) == 0
    assert adapter.calls == []
    assert {p: p.read_bytes() for p in snapshot} == snapshot
    assert "export: would write 4 new game(s)" in capsys.readouterr().out


def test_limit_then_resume_reaches_the_same_outputs(tmp_path, legacy_csv, base_run):
    ref_dir, _, _ = base_run
    work = tmp_path / "limited"
    work.mkdir()
    adapter = steam()
    assert cmd(work, legacy_csv, adapter, "run", "--end-date", "2026-10-30", "--limit", "2") == 0
    assert len(rows(work / pipeline.NEW_CSV)) - 1 <= 2
    for _ in range(4):
        cmd(work, legacy_csv, adapter, "run", "--resume", "--limit", "2")
    assert (work / pipeline.NEW_CSV).read_bytes() == (ref_dir / pipeline.NEW_CSV).read_bytes()
    assert (work / pipeline.MERGED_CSV).read_bytes() == (ref_dir / pipeline.MERGED_CSV).read_bytes()


def test_interrupted_crawl_resumes_safely(tmp_path, legacy_csv, base_run, capsys):
    ref_dir, _, _ = base_run
    work = tmp_path / "interrupted"
    work.mkdir()
    adapter = steam()
    adapter.tags[9900003] = KeyboardInterrupt()
    assert cmd(work, legacy_csv, adapter, "run", "--end-date", "2026-10-30") == cli.EXIT_INTERRUPTED
    status = json.loads((work / "work" / "crawl_status.json").read_text())
    assert status["stages"]["crawl"]["state"] == "interrupted"
    assert not (work / pipeline.NEW_CSV).exists()

    adapter.tags[9900003] = []
    adapter.calls.clear()
    assert cmd(work, legacy_csv, adapter, "run", "--resume") == 0, capsys.readouterr().out
    assert calls(adapter, "/IStoreService/GetAppList/v1/") == []  # discovery was already finished
    assert (work / pipeline.NEW_CSV).read_bytes() == (ref_dir / pipeline.NEW_CSV).read_bytes()


def test_unfinished_discovery_requires_resume(tmp_path, legacy_csv):
    adapter = steam()
    cmd(tmp_path, legacy_csv, adapter, "discover", "--limit", "1")
    assert cmd(tmp_path, legacy_csv, adapter, "run") == cli.EXIT_USAGE
    assert cmd(tmp_path, legacy_csv, adapter, "run", "--resume") == 0


# --- safety ---------------------------------------------------------------------------------------------

def test_workspace_lock_blocks_a_second_crawler(ran, capsys):
    tmp_path, legacy_csv, adapter, _ = ran
    ws = pipeline.Workspace.at(tmp_path, tmp_path / "work", legacy_csv)
    with pipeline.workspace_lock(ws):
        assert cmd(tmp_path, legacy_csv, adapter, "crawl") == cli.EXIT_ERROR
    assert "another crawler process" in capsys.readouterr().out
    assert cmd(tmp_path, legacy_csv, adapter, "crawl") == 0  # released


def test_recovery_removes_interrupted_temp_files(ran, capsys):
    tmp_path, legacy_csv, adapter, _ = ran
    leftover = tmp_path / f".{pipeline.MERGED_CSV}.tmp"
    leftover.write_bytes(b"partial")
    assert cmd(tmp_path, legacy_csv, adapter, "export") == 0
    assert not leftover.exists() and "removed interrupted temp file" in capsys.readouterr().out


def test_corrupt_state_is_refused_not_overwritten(ran, capsys):
    tmp_path, legacy_csv, adapter, _ = ran
    db = tmp_path / "work" / "metadata.sqlite3"
    for side in ("-wal", "-shm"):
        (tmp_path / "work" / f"metadata.sqlite3{side}").unlink(missing_ok=True)
    db.write_bytes(b"not a database" * 100)
    assert cmd(tmp_path, legacy_csv, adapter, "crawl") == cli.EXIT_ERROR
    assert "failed integrity check" in capsys.readouterr().out
    assert db.read_bytes().startswith(b"not a database")  # left for the user to inspect
    assert cmd(tmp_path, legacy_csv, adapter, "audit") == cli.EXIT_ERROR


def test_audit_is_read_only(tmp_path, legacy_csv, capsys):
    assert cmd(tmp_path, legacy_csv, steam(), "audit", environ={}) == 0
    out = capsys.readouterr().out
    assert "[ OK ] legacy CSV sha256" in out and "23034 rows, 0 violation(s)" in out
    assert not (tmp_path / "work").exists()


def test_export_refuses_modified_legacy_csv(tmp_path, legacy_csv, capsys):
    copy = tmp_path / "legacy_copy.csv"
    copy.write_bytes(legacy_csv.read_bytes().replace(b"\r\n", b"\n"))
    adapter = steam()
    cmd(tmp_path, copy, adapter, "discover", "--end-date", "2026-10-30")
    assert cmd(tmp_path, copy, adapter, "export") == cli.EXIT_ERROR
    assert "does not match the expected sha256" in capsys.readouterr().out


# --- validate catches tampering --------------------------------------------------------------------------

def _tamper(path, old, new):
    data = path.read_bytes()
    assert old in data
    path.write_bytes(data.replace(old, new, 1))


@pytest.mark.parametrize(
    ("name", "old", "new", "message"),
    [
        (pipeline.MERGED_CSV, b"Halo: The Master Chief Collection", b"Halo: The Master Chief Collectio", "byte-for-byte"),
        (pipeline.NEW_CSV, b",metascore\r\n", b",metascore,extra\r\n", "header differs"),
        (pipeline.NEW_CSV, b"\r\n9900002,", b"\r\n9900001,", "duplicate AppIDs"),
        (pipeline.PROVENANCE_CSV, b"Example Publishing", b"Example Publishin", "disagree"),
        (pipeline.NEW_CSV, b"\r\n9900002,", b"\r\n976730,", "legacy AppIDs"),
    ],
)
def test_validate_detects_tampering(ran, capsys, name, old, new, message):
    tmp_path, legacy_csv, adapter, _ = ran
    _tamper(tmp_path / name, old, new)
    capsys.readouterr()
    assert cmd(tmp_path, legacy_csv, adapter, "validate") == cli.EXIT_ERROR
    out = capsys.readouterr().out
    assert message in out and "manifest hash mismatch" in out


def test_validate_requires_all_outputs(ran, capsys):
    tmp_path, legacy_csv, adapter, _ = ran
    (tmp_path / pipeline.MANIFEST_JSON).unlink()
    assert cmd(tmp_path, legacy_csv, adapter, "validate") == cli.EXIT_ERROR


# --- edge cases --------------------------------------------------------------------------------------------

def test_no_new_games_merged_equals_legacy(tmp_path, legacy_csv):
    adapter = SampleSteam(catalog={LEGACY_IN_CATALOG: 1})
    assert cmd(tmp_path, legacy_csv, adapter, "run") == 0
    assert rows(tmp_path / pipeline.NEW_CSV) == [list(schema.COLUMNS)]
    assert (tmp_path / pipeline.MERGED_CSV).read_bytes() == legacy_csv.read_bytes()


def test_window_end_excludes_later_games(tmp_path, legacy_csv):
    assert cmd(tmp_path, legacy_csv, steam(), "run", "--end-date", "2022-12-31") == 0
    assert [int(r[0]) for r in rows(tmp_path / pipeline.NEW_CSV)[1:]] == [9900003, 9900004]


def test_seed_run_without_key(tmp_path, legacy_csv):
    adapter = steam()
    argv = ["run", "--end-date", "2026-10-30", "--appid", "9900001", "--appid", str(LEGACY_IN_CATALOG)]
    code = cmd(tmp_path, legacy_csv, adapter, *argv, environ={})
    assert code == 0
    assert calls(adapter, "/IStoreService/GetAppList/v1/") == []
    assert [int(r[0]) for r in rows(tmp_path / pipeline.NEW_CSV)[1:]] == [9900001]
    manifest = json.loads((tmp_path / pipeline.MANIFEST_JSON).read_text())
    assert manifest["discovery"]["mode"] == "seed"
    assert manifest["reviews"]["sentiment_filled"] == 1  # the fake lists GetAppReviews publicly


def test_reviews_unavailable_leaves_sentiment_blank_but_completes(tmp_path, legacy_csv, capsys):
    adapter = steam(review_params=None)
    assert cmd(tmp_path, legacy_csv, adapter, "run", "--end-date", "2026-10-30") == 0
    assert "review summaries skipped" in capsys.readouterr().out
    assert all(r[10] == "" for r in rows(tmp_path / pipeline.NEW_CSV)[1:])


def test_enable_steamspy(tmp_path, legacy_csv):
    adapter = steam()
    assert cmd(tmp_path, legacy_csv, adapter, "run", "--end-date", "2026-10-30", "--enable-steamspy") == 0
    new = {int(r[0]): r for r in rows(tmp_path / pipeline.NEW_CSV)[1:]}
    assert new[9900001][8].startswith("['Action', 'Indie', 'Point & Click', 'Adventure'")
    assert (tmp_path / "work" / "cache" / "steamspy.sqlite3").exists()
    assert all("key" not in q for q in calls(adapter, "/api.php"))

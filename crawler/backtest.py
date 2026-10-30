"""Back-test: re-extract games that are already in steamproducts2020.csv and compare structure.

The goal is structural compatibility (does today's extraction produce cells in
the same representation as the 2020 file?), not equality of values, which
legitimately change. The back-test uses its own workspace (``work/backtest``)
and never feeds the new or merged datasets.
"""

from __future__ import annotations

import collections
import csv
import io
import json
import random
import re
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from crawler import extract, pipeline, schema
from crawler import reviews as rv
from crawler.cache import ResponseCache
from crawler.discovery_state import iso
from crawler.metadata import with_review
from crawler.release_dates import parse_release_date
from crawler.steam_api import SteamWebAPI
from crawler.transport import SteamAuthError

DEFAULT_SAMPLE = 25
DEFAULT_SEED = 2020
COMPARISON_FIELDS = ("appid", "stratum", "column", "legacy_value", "current_value", "compatible", "equal",
                     "current_status", "note")
LIST_COLUMNS = ("genres", "specs", "tags")
_FEW = re.compile(r"[1-9] user reviews")

# Strata of interesting legacy cases; each contributes up to an equal share of the sample.
STRATA: dict[str, Any] = {
    "early_access": lambda r: r["early_access"] == "TRUE",
    "free": lambda r: r["price"] == "0",
    "metascore": lambda r: r["metascore"] != "",
    "few_reviews": lambda r: bool(_FEW.fullmatch(r["sentiment"])),
    "free_text_date": lambda r: schema.parse_release_date(r["release_date"]) is None,
    "random": lambda r: True,
}


def select_sample(legacy_csv: Path, n: int = DEFAULT_SAMPLE, seed: int = DEFAULT_SEED) -> list[tuple[int, str]]:
    """A deterministic stratified sample of (appid, stratum) from the original CSV."""
    rows = sorted(schema.read_rows(legacy_csv), key=lambda r: int(r["id"]))
    rng = random.Random(seed)
    per = max(1, n // len(STRATA))
    chosen: dict[int, str] = {}
    for name, pred in STRATA.items():
        pool = [int(r["id"]) for r in rows if pred(r) and int(r["id"]) not in chosen]
        for appid in rng.sample(pool, min(per, len(pool))):
            chosen[appid] = name
    rest = [int(r["id"]) for r in rows if int(r["id"]) not in chosen]
    for appid in rng.sample(rest, max(0, n - len(chosen))):
        chosen[appid] = "random"
    return list(chosen.items())[:n]


def _decoded(column: str, cell: str) -> Any:
    return schema.DECODERS[column](cell)


def compare_cell(column: str, old: str, new: str) -> tuple[bool, bool, str]:
    """(compatible, equal, note) for one column of one game.

    Compatible: the current cell is valid under the column's contract and, when
    both cells are present, decodes to the same Python type as the 2020 cell.
    """
    try:
        new_value = _decoded(column, new)
    except ValueError as exc:
        return False, old == new, f"current cell invalid: {exc}"
    old_value = _decoded(column, old)
    if new_value is not None and old_value is not None and type(new_value) is not type(old_value):
        return False, False, f"type changed {type(old_value).__name__} -> {type(new_value).__name__}"
    note = ""
    if column == "release_date" and old and new:
        po, pn = parse_release_date(old), parse_release_date(new)
        if po is None:
            note = "2020 free text"
        elif pn is not None and (po.earliest, po.latest) == (pn.earliest, pn.latest):
            note = "same date"
        else:
            note = "date differs"
    return True, old == new, note


def _row_serializable(row: Mapping[str, str]) -> bool:
    data = pipeline._csv_bytes([schema.COLUMNS]) + pipeline._csv_bytes([[row[c] for c in schema.COLUMNS]])
    parsed = list(csv.reader(io.StringIO(data.decode(), newline="")))
    return parsed[1] == [row[c] for c in schema.COLUMNS] and not schema.validate_rows(parsed)


def run_backtest(ctx: pipeline.Context, sample: Sequence[tuple[int, str]], *, refresh: bool = False,
                 out_dir: Path | None = None, seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """Extract ``sample`` live into ``work/backtest`` and write the comparison files to ``reports/``."""
    ws, log = ctx.ws, ctx.log
    bt_dir = ws.work / "backtest"
    out_dir = out_dir or ws.base / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    legacy = {int(r["id"]): r for r in schema.read_rows(ws.legacy_csv)}
    appids = [a for a, _ in sample]
    stratum = dict(sample)
    transport = ctx.get_transport()
    key = ctx.key()
    with ResponseCache(bt_dir / "appdetails.sqlite3") as cache, \
            extract.ExtractionState(bt_dir / "metadata.sqlite3") as state:
        store = pipeline._store(ctx, cache)
        # legacy_ids is empty on purpose: these AppIDs are *meant* to be in the original CSV.
        extractor = extract.Extractor(state, web=SteamWebAPI(transport, key), store=store, legacy_ids=frozenset(),
                                      now=ctx.now, log=log)
        report = extractor.run(appids, refresh=refresh)
        rows = {int(r["id"]): (r, p) for r, p in state.rows()}
        failures = {int(f["appid"]): f for f in state.failures()}
    reviews: dict[int, rv.ReviewRecord] = {}
    reviews_note = ""
    with rv.ReviewState(bt_dir / "reviews.sqlite3") as rstate:
        try:
            rv.ReviewCollector(rstate, SteamWebAPI(transport, key), now=ctx.now, log=log).run(appids, refresh=refresh)
            reviews = rstate.records()
        except (rv.ReviewEndpointUnavailable, SteamAuthError) as exc:
            reviews_note = str(exc)
    rows = {a: with_review(r, p, reviews.get(a)) for a, (r, p) in rows.items()}

    comparison: list[dict[str, str]] = []
    columns: dict[str, dict[str, Any]] = {c: collections.Counter() for c in schema.COLUMNS}
    vocab = {c: {v for r in legacy.values() if r[c] for v in schema.decode_list(r[c])} for c in LIST_COLUMNS}
    vocab_hits = {c: [0, 0] for c in LIST_COLUMNS}
    price_deltas = []
    # Which cents -> dollars rule reproduces the 2020 price, for games with a current regular price?
    rounding = collections.Counter()
    rows_valid = rows_serializable = 0
    for appid in appids:
        if appid not in rows:
            continue
        row, prov = rows[appid]
        status = {p["field"]: p["status"] for p in prov}
        rows_valid += not schema.validate_row(row)
        rows_serializable += _row_serializable(row)
        old_row = legacy[appid]
        for c in schema.COLUMNS:
            ok, equal, note = compare_cell(c, old_row[c], row[c])
            s = columns[c]
            s["n"] += 1
            s["compatible"] += ok
            s["equal"] += equal
            s["blank_now"] += row[c] == ""
            s["blank_then"] += old_row[c] == ""
            if note:
                s[f"note:{note}"] += 1
            comparison.append({"appid": str(appid), "stratum": stratum[appid], "column": c,
                               "legacy_value": old_row[c], "current_value": row[c], "compatible": str(ok).lower(),
                               "equal": str(equal).lower(), "current_status": status.get(c, ""), "note": note})
        for c in LIST_COLUMNS:
            for v in schema.decode_list(row[c]) or []:
                vocab_hits[c][1] += 1
                vocab_hits[c][0] += v in vocab[c]
        if row["price"] and old_row["price"]:
            price_deltas.append(int(row["price"]) - int(old_row["price"]))
        price_raw = next((json.loads(p["raw"]) for p in prov if p["field"] == "price" and p["raw"]), {})
        initial = (price_raw.get("price_overview") or {}).get("initial")
        if isinstance(initial, int) and initial > 0 and old_row["price"]:
            old = int(old_row["price"])
            by_round, by_floor = old == (initial + 50) // 100, old == initial // 100
            rounding["both" if by_round and by_floor else "round_only" if by_round else
                     "floor_only" if by_floor else "neither"] += 1

    def note_for(c: str) -> str:
        s = columns[c]
        parts = [f"{k[5:]}={v}" for k, v in sorted(s.items()) if k.startswith("note:")]
        if c in LIST_COLUMNS and vocab_hits[c][1]:
            hit, total = vocab_hits[c]
            parts.append(f"{100 * hit / total:.0f}% of current labels exist in the 2020 vocabulary")
        if c == "price" and price_deltas:
            parts.append(f"median change {statistics.median(price_deltas):+g} USD")
        if c == "sentiment" and reviews_note:
            parts.append("review summaries unavailable without a key")
        if c == "early_access":
            parts.append("blank = unknown (no explicit store flag)")
        if c == "publisher":
            parts.append("2020 'Franchise:' suffixes are not reproduced")
        return "; ".join(parts)

    summary = {
        "run_at": iso(ctx.now()), "seed": seed, "sample": len(appids),
        "strata": dict(collections.Counter(stratum.values())),
        "extracted": len(rows), "unavailable": len(appids) - len(rows),
        "unavailable_reasons": [f"{a}: {failures[a]['error']}" for a in appids if a in failures],
        "rows_valid": rows_valid, "rows_serializable": rows_serializable,
        "partial_rows": report.partial, "reviews_note": reviews_note,
        "price_rule_evidence": {k: rounding[k] for k in ("round_only", "floor_only", "both", "neither")},
        "requests_sent": transport.requests_sent,
        "columns": {c: {"n": columns[c]["n"], "compatible": columns[c]["compatible"], "equal": columns[c]["equal"],
                        "blank_now": columns[c]["blank_now"], "blank_then": columns[c]["blank_then"],
                        "note": note_for(c)} for c in schema.COLUMNS},
    }
    pipeline._atomic_write_bytes(out_dir / "backtest_comparison.csv", _comparison_bytes(comparison))
    pipeline._atomic_write_bytes(out_dir / "backtest_summary.json",
                                 (json.dumps(summary, indent=2, sort_keys=True) + "\n").encode())
    return summary


def _comparison_bytes(rows: Sequence[Mapping[str, str]]) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=COMPARISON_FIELDS, lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")

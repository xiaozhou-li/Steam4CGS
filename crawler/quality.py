"""Field-level quality checks of the final outputs and the Markdown quality report.

``check_outputs`` re-reads the files on disk (never the in-memory state that
produced them) and returns one :class:`Check` per rule. ``render_report``
turns the checks, coverage and missingness statistics, exclusions, failures
and an optional back-test into ``reports/quality_report.md``.
"""

from __future__ import annotations

import collections
import csv
import datetime as dt
import hashlib
import io
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crawler import pipeline, schema
from crawler.discovery_state import DiscoveryState, iso
from crawler.eligibility import REASONS
from crawler.metadata import MISSING, NOT_COLLECTED, PRESENT, SOURCE_UNAVAILABLE, UNRECOGNIZED, UNVERIFIED
from crawler.release_dates import parse_release_date

_UINT = re.compile(r"0|[1-9]\d*")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    failures: list[str] = field(default_factory=list)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def _check(name: str, failures: Sequence[str], ok_detail: str) -> Check:
    return Check(name, not failures, ok_detail if not failures else f"{len(failures)} problem(s)", list(failures)[:20])


def _rid(row: Mapping[str, str]) -> int:
    """The row's AppID, or -1 when the id cell is not a number (reported by the contract check)."""
    value = row.get("id", "")
    return int(value) if value.isdigit() else -1


def _genre_order_ok(genres: list[str]) -> bool:
    rest = [g for g in genres if g != schema.EARLY_ACCESS_GENRE]
    expected = sorted(rest, key=str.casefold) + ([schema.EARLY_ACCESS_GENRE] if schema.EARLY_ACCESS_GENRE in genres else [])
    return genres == expected


def check_outputs(ws: pipeline.Workspace) -> list[Check]:
    """Every rule of the output contract, evaluated on the files themselves."""
    checks: list[Check] = []
    legacy_bytes = ws.legacy_csv.read_bytes()
    legacy_header = legacy_bytes.split(b"\r\n", 1)[0]
    legacy_ids = {_rid(r) for r in schema.read_rows(ws.legacy_csv)}
    new_bytes = ws.new_csv.read_bytes()
    # Parse defensively: a malformed file must produce failed checks, never an exception.
    new_text = new_bytes.decode("utf-8", errors="replace").removeprefix("\ufeff")
    parsed = list(csv.reader(io.StringIO(new_text, newline="")))
    header = parsed[0] if parsed else []
    rows = [dict(zip(header, r)) for r in parsed[1:]]

    # 1. legacy file
    digest = hashlib.sha256(legacy_bytes).hexdigest()
    checks.append(_check("original CSV unchanged", [] if digest == pipeline.LEGACY_SHA256 else
                         [f"sha256 {digest} != {pipeline.LEGACY_SHA256}"], f"sha256 {digest}"))

    # 2. exact schema
    fails = []
    if tuple(header) != schema.COLUMNS:
        fails.append(f"columns {header}")
    if new_bytes.split(b"\r\n", 1)[0] != legacy_header:
        fails.append("header bytes differ from steamproducts2020.csv")
    fails += [f"row {i}: {len(r)} fields" for i, r in enumerate(parsed) if len(r) != 13]
    checks.append(_check("exact 13-column schema", fails, "header identical to the original, 13 fields per row"))
    header_ok = not fails

    # 3. serialization
    fails = []
    if new_bytes.startswith(schema.UTF8_BOM):
        fails.append("UTF-8 BOM present")
    try:
        text = new_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        text = ""
        fails.append(f"not UTF-8: {exc}")
    if text.count("\n") != text.count("\r\n") or text.count("\r") != text.count("\r\n"):
        fails.append("line terminators are not all CRLF")
    if text and not text.endswith("\r\n"):
        fails.append("last record is not CRLF-terminated")
    reserialized = pipeline._csv_bytes(parsed)
    if reserialized != new_bytes:
        fails.append("bytes differ from csv.writer(QUOTE_MINIMAL, CRLF) re-serialization")
    fails += [str(v) for v in schema.validate_file(ws.new_csv)]
    checks.append(_check("CSV serialization", fails, "UTF-8 without BOM, CRLF, minimal quoting, round-trips byte-exactly"))
    if not header_ok:
        checks.append(Check("row-level checks", False, "skipped: the header or field counts are invalid",
                            ["fix the schema problems above first"]))
        return checks
    ids = [_rid(r) for r in rows]

    # 4. AppIDs
    fails = [f"duplicate {a}" for a, n in collections.Counter(ids).items() if n > 1]
    fails += [f"{a} is in steamproducts2020.csv" for a in ids if a in legacy_ids]
    merged_ids = [int(r[0]) for r in list(csv.reader(io.StringIO(ws.merged_csv.read_bytes().decode(), newline="")))[1:]]
    fails += [f"merged duplicate {a}" for a, n in collections.Counter(merged_ids).items() if n > 1]
    checks.append(_check("unique AppIDs", fails, f"{len(ids)} unique new AppIDs, none in the original; "
                                                 f"{len(merged_ids)} unique in merged"))

    # 5. release-date bounds
    eligible, run = pipeline.eligible_appids(ws, frozenset(legacy_ids))
    fails = []
    precision = collections.Counter()
    if run is None:
        fails.append("no discovery state: window unknown")
    else:
        for r in rows:
            parsed = parse_release_date(r["release_date"])
            if parsed is None:
                fails.append(f"{r['id']}: unparsed release_date {r['release_date']!r}")
                continue
            precision[parsed.precision] += 1
            if parsed.earliest < run.window.start or parsed.latest > run.window.end:
                fails.append(f"{r['id']}: {r['release_date']!r} outside {run.window}")
            if parsed.exact is not None and r["release_date"] != schema.encode_release_date(parsed.exact):
                fails.append(f"{r['id']}: exact date not in 'D Mon, YYYY' form")
    window = f"window {run.window}" if run else "no window"
    checks.append(_check("release dates within the window", fails,
                         f"{window}; precision " + ", ".join(f"{k}={v}" for k, v in sorted(precision.items()))))

    # 6. integer prices, from the regular listing price
    prices = {}
    if (ws.metadata_out / "prices.csv").exists():
        prices = {int(p["appid"]): p for p in _read_csv(ws.metadata_out / "prices.csv")[1]}
    fails = []
    for r in rows:
        cell = r["price"]
        if cell and not _UINT.fullmatch(cell):
            fails.append(f"{r['id']}: price {cell!r} is not a non-negative integer")
            continue
        p = prices.get(_rid(r))
        if p is None:
            fails.append(f"{r['id']}: no price provenance")
            continue
        if p["is_free"] == "true":
            expected = "0"
        elif p["initial_cents"]:
            expected = str(schema.price_from_cents(int(p["initial_cents"])))
        elif p["final_cents"] and p["discount_percent"] in ("", "0"):
            expected = str(schema.price_from_cents(int(p["final_cents"])))
        else:
            expected = ""
        if cell != expected:
            fails.append(f"{r['id']}: price {cell!r}, expected {expected!r} from the regular price")
        if p["currency"] not in ("", "USD"):
            fails.append(f"{r['id']}: currency {p['currency']}")
    on_sale = sum(1 for p in prices.values() if p["discount_percent"] not in ("", "0"))
    checks.append(_check("integer USD prices from the regular price", fails,
                         f"{sum(1 for r in rows if r['price'])} priced, {on_sale} on sale at crawl time (regular price used)"))

    # 7. list formatting
    fails = []
    for r in rows:
        decoded = {}
        for c in ("genres", "specs", "tags"):
            try:
                decoded[c] = schema.decode_list(r[c]) or []
            except ValueError as exc:
                fails.append(f"{r['id']}.{c}: {exc}")
                decoded[c] = []
        if decoded["genres"] and not _genre_order_ok(decoded["genres"]):
            fails.append(f"{r['id']}.genres: not in legacy order")
        if len(decoded["tags"]) > 20:
            fails.append(f"{r['id']}.tags: more than 20")
    checks.append(_check("list formatting", fails, "canonical Python-list repr; genres in legacy order; <= 20 tags"))

    # 8. booleans
    ea = {}
    if (ws.metadata_out / "early_access.csv").exists():
        ea = {int(e["appid"]): e for e in _read_csv(ws.metadata_out / "early_access.csv")[1]}
    fails = []
    for r in rows:
        cell, evidence = r["early_access"], ea.get(_rid(r))
        if cell not in ("TRUE", "FALSE", ""):
            fails.append(f"{r['id']}: early_access {cell!r}")
        if evidence is None:
            fails.append(f"{r['id']}: no Early Access evidence row")
        elif {"TRUE": "true", "FALSE": "false", "": "unknown"}.get(cell) != evidence["status"]:
            fails.append(f"{r['id']}: cell {cell!r} vs evidence {evidence['status']}")
        if cell == "FALSE" and evidence and evidence["store_item_is_early_access"] != "false":
            fails.append(f"{r['id']}: FALSE without an explicit is_early_access=false")
    dist = collections.Counter(r["early_access"] or "unknown" for r in rows)
    checks.append(_check("early_access booleans", fails, ", ".join(f"{k}={v}" for k, v in sorted(dist.items()))
                         + "; FALSE only from an explicit store flag"))

    # 9. missing-data handling, against provenance
    pheader, prov_rows = _read_csv(ws.provenance_csv)
    prov = {(int(p["appid"]), p["field"]): p for p in prov_rows}
    fails = []
    for r in rows:
        for c in schema.COLUMNS:
            p = prov.get((_rid(r), c))
            if p is None:
                fails.append(f"{r['id']}.{c}: no provenance")
            elif p["value"] != r[c]:
                fails.append(f"{r['id']}.{c}: provenance value differs")
            elif (r[c] == "") == (p["status"] == PRESENT):
                fails.append(f"{r['id']}.{c}: cell {'blank' if not r[c] else 'filled'} but status {p['status']}")
    reviews = {}
    if (ws.reviews_out / "review_summaries.csv").exists():
        reviews = {int(x["appid"]): x for x in _read_csv(ws.reviews_out / "review_summaries.csv")[1]}
    for r in rows:
        if r["sentiment"] and reviews.get(_rid(r), {}).get("status") != "ok":
            fails.append(f"{r['id']}: sentiment without a verified review summary")
    checks.append(_check("missing-data handling", fails,
                         "blank <=> provenance status != present; sentiment only from verified summaries; "
                         "no value substituted from another field"))

    # 10. metascore
    fails = [f"{r['id']}: metascore {r['metascore']!r}" for r in rows
             if r["metascore"] and not (_UINT.fullmatch(r["metascore"]) and int(r["metascore"]) <= 100)]
    checks.append(_check("metascore integers 0-100", fails, f"{sum(1 for r in rows if r['metascore'])} present"))

    # 11. merged file
    merged_bytes = ws.merged_csv.read_bytes()
    fails = []
    if not merged_bytes.startswith(legacy_bytes):
        fails.append("legacy bytes are not an exact prefix")
    body = new_bytes.split(b"\r\n", 1)[1] if b"\r\n" in new_bytes else b""
    expected_tail = (b"\r\n" + body) if rows else b""
    if merged_bytes[len(legacy_bytes):] != expected_tail:
        fails.append("appended bytes differ from the new CSV's rows")
    checks.append(_check("merged = original bytes + new rows", fails,
                         f"{len(legacy_bytes)} original bytes preserved, {len(rows)} rows appended"))

    # 12. pipeline validate (manifest hashes, eligibility, failed file)
    lines: list[str] = []
    ctx = pipeline.Context(ws, {}, log=lines.append)
    try:
        code = pipeline.validate(ctx)
        problems = [l for l in lines if l.startswith("[FAIL]")] if code else []
    except Exception as exc:  # a checker must report, never crash
        problems = [f"validate raised {type(exc).__name__}: {exc}"]
    checks.append(_check("pipeline validate", problems, "manifest hashes, eligibility and failed-AppID checks passed"))
    return checks


# --------------------------------------------------------------------------
# Statistics for the report
# --------------------------------------------------------------------------


def missingness(ws: pipeline.Workspace) -> list[dict[str, Any]]:
    _, rows = _read_csv(ws.new_csv)
    _, prov_rows = _read_csv(ws.provenance_csv)
    statuses = collections.defaultdict(collections.Counter)
    for p in prov_rows:
        if p["value"] == "":
            statuses[p["field"]][p["status"]] += 1
    out = []
    for c in schema.COLUMNS:
        blank = sum(1 for r in rows if r[c] == "")
        out.append({"column": c, "filled": len(rows) - blank, "blank": blank,
                    "pct": 0.0 if not rows else 100 * blank / len(rows), "reasons": dict(statuses[c])})
    return out


def coverage(ws: pipeline.Workspace) -> dict[str, Any]:
    legacy_ids = frozenset(int(r["id"]) for r in schema.read_rows(ws.legacy_csv))
    manifest = json.loads(ws.manifest.read_text(encoding="utf-8")) if ws.manifest.exists() else {}
    exclusions = collections.Counter()
    excluded_csv = ws.discovery_out / "excluded.csv"
    if excluded_csv.exists():
        exclusions.update(r["reason"] for r in _read_csv(excluded_csv)[1])
    _, failed = _read_csv(ws.failed_csv) if ws.failed_csv.exists() else ([], [])
    _, prov_rows = _read_csv(ws.provenance_csv)
    partial = collections.Counter((p["field"], p["source"].split(":")[0]) for p in prov_rows
                                  if p["status"] == SOURCE_UNAVAILABLE)
    discovered = 0
    if ws.discovery_state.exists():
        with DiscoveryState(ws.discovery_state) as st:
            discovered = sum(1 for _ in st.iter_discovered())
    return {"manifest": manifest, "exclusions": exclusions, "failed": failed, "partial": partial,
            "discovered": discovered, "legacy_rows": len(legacy_ids)}


SOURCE_LIMITATIONS = {
    "IStoreService/GetAppList/v1": [
        "Requires a Steam Web API key; without one, discovery runs only in seed mode (explicit AppIDs).",
        "Carries no release dates, so every candidate needs one appdetails request.",
    ],
    "Store appdetails (undocumented)": [
        "Unversioned; fields can change or disappear without notice.",
        "cc=us: apps not sold in the US return success=false (excluded as store_details_unavailable).",
        "release_date is the current store date (the 1.0 date for games that have left Early Access).",
        "No franchise field, so the legacy 'Franchise:' publisher suffix is never produced.",
        "Genre/category vocabulary has drifted since 2020 (e.g. 'Free To Play', accessibility categories).",
        "Metacritic scores are often absent.",
    ],
    "IStoreBrowseService/GetItems/v1": [
        "is_early_access is omitted (not sent as false) for games that are not in Early Access, so "
        "early_access is TRUE or unknown; FALSE needs an explicit flag.",
        "Tag weights are Steam's internal ranking, not vote counts.",
    ],
    "IStoreService/GetTagList/v1": ["Tag names are localized; English is requested."],
    "IUserReviewsService/GetAppReviews/v1": [
        "Not in the public method list; visible (if at all) only with a key. Without it, sentiment is blank.",
        "Request parameter names and the response shape are verified per response; failures leave sentiment blank.",
    ],
    "SteamSpy (optional)": [
        "Third-party estimates; can lag the store by a day or more; documented limit 1 request/second.",
    ],
}


def render_report(ws: pipeline.Workspace, checks: Sequence[Check], *, backtest: Mapping[str, Any] | None = None,
                  tests: str | None = None, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    cov = coverage(ws)
    m = cov["manifest"]
    miss = missingness(ws)
    lines: list[str] = []
    w = lines.append

    w("# Data quality report")
    w("")
    w(f"Generated {iso(now)} by `python -m crawler quality` from the files on disk. "
      f"Git commit: `{(m.get('tool') or {}).get('git_commit') or 'unknown'}`.")
    w("")
    disc = m.get("discovery") or {}
    win = m.get("window") or {}
    w(f"* **Window:** {win.get('start')} to {win.get('end')}")
    w(f"* **Discovery mode:** `{disc.get('mode')}`")
    w(f"* **New games:** {(m.get('crawl') or {}).get('new_games')}")
    w(f"* **Original rows:** {cov['legacy_rows']}")
    if disc.get("mode") == "seed":
        w("")
        w("> **Seed mode:** this dataset comes from explicit AppIDs, not the `GetAppList` catalog.")
        w("> Coverage below describes those seeds, not the whole Steam catalog.")
    w("")

    w("## Validation checks")
    w("")
    w("| Check | Result | Detail |")
    w("|---|---|---|")
    for c in checks:
        w(f"| {c.name} | {'PASS' if c.ok else '**FAIL**'} | {c.detail} |")
    for c in checks:
        if c.failures:
            w("")
            w(f"**{c.name}** failures (first {len(c.failures)}):")
            w("")
            for f in c.failures:
                w(f"* {f}")
    w("")

    w("## Collection coverage")
    w("")
    counts = disc.get("counts") or {}
    crawl = m.get("crawl") or {}
    rev = m.get("reviews") or {}
    w("| Stage | Count |")
    w("|---|---|")
    w(f"| AppIDs discovered | {cov['discovered']} |")
    for k in ("eligible", "excluded", "failed", "pending"):
        w(f"| Discovery {k} | {counts.get(k, 0)} |")
    w(f"| Eligible but not crawled yet | {crawl.get('eligible_not_yet_crawled', 0)} |")
    w(f"| New games in `{pipeline.NEW_CSV}` | {crawl.get('new_games', 0)} |")
    review_counts = rev.get("statuses") or {}
    w(f"| Review summaries collected | {sum(review_counts.values())} "
      f"({', '.join(f'{k}={v}' for k, v in sorted(review_counts.items())) or 'none'}) |")
    w(f"| Rows with sentiment | {rev.get('sentiment_filled', 0)} |")
    w("")

    w("## Missingness per field")
    w("")
    w("A blank cell is always explained by a provenance status. Statuses:")
    w("")
    w(f"* `{MISSING}`: the source has no value.")
    w(f"* `{UNVERIFIED}`: there is no explicit evidence.")
    w(f"* `{UNRECOGNIZED}`: the value doesn't fit the contract.")
    w(f"* `{SOURCE_UNAVAILABLE}`: the request failed.")
    w(f"* `{NOT_COLLECTED}`: the review summary was not collected.")
    w("")
    w("| Column | Filled | Blank | Blank % | Why blank |")
    w("|---|---|---|---|---|")
    for r in miss:
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(r["reasons"].items())) or "—"
        w(f"| `{r['column']}` | {r['filled']} | {r['blank']} | {r['pct']:.1f} | {reasons} |")
    w("")

    w("## Exclusions")
    w("")
    if cov["exclusions"]:
        w("| Reason | Count | Rule |")
        w("|---|---|---|")
        for reason, n in cov["exclusions"].most_common():
            w(f"| `{reason}` | {n} | {REASONS.get(reason, ('', ''))[1]} |")
    else:
        w("None.")
    w("")

    w("## API failures")
    w("")
    if cov["failed"]:
        w("| AppID | Stage | Error | Attempts | Detail |")
        w("|---|---|---|---|---|")
        for f in cov["failed"]:
            w(f"| {f['appid']} | {f['stage']} | {f['error']} | {f['attempts']} | {f['detail'][:120]} |")
    else:
        w("No AppID failed at any stage (`work/failed_appids.csv` is empty).")
    if cov["partial"]:
        w("")
        w("Fields left blank because their source request failed (the rows are retried on the next run):")
        w("")
        for (fld, src), n in sorted(cov["partial"].items()):
            w(f"* `{fld}` from `{src}`: {n}")
    w("")

    if backtest:
        _render_backtest(w, backtest)

    w("## Source-specific limitations")
    w("")
    for source, items in SOURCE_LIMITATIONS.items():
        w(f"**{source}**")
        w("")
        for item in items:
            w(f"* {item}")
        w("")

    if tests:
        w("## Test suite")
        w("")
        w("```")
        w(tests.strip())
        w("```")
        w("")

    w("## Reproduce")
    w("")
    w("```sh")
    w("python3.13 -m pytest                                   # offline test suite (mocked APIs)")
    w("python3.13 -m crawler backtest --sample 25             # live back-test on original games")
    w("python3.13 -m crawler quality --run-tests              # regenerate this report")
    w("```")
    return "\n".join(lines) + "\n"


def _render_backtest(w, bt: Mapping[str, Any]) -> None:
    w("## Back-test on games from the original CSV")
    w("")
    w(f"{bt['sample']} AppIDs from `steamproducts2020.csv` (deterministic stratified sample, seed {bt['seed']}) were "
      f"re-extracted with the current pipeline on {bt['run_at']}. This compares **structural compatibility**: "
      "whether today's cells use the same representation as the 2020 cells. It does not compare historical "
      "values, which legitimately change.")
    w("")
    w(f"* Still on the store and extracted: **{bt['extracted']}**")
    w(f"* Not extractable (delisted, region-locked or failed): {bt['unavailable']}"
      + (f" ({', '.join(bt['unavailable_reasons'])})" if bt['unavailable_reasons'] else ""))
    w(f"* Rows passing the full 13-column contract: **{bt['rows_valid']}/{bt['extracted']}**")
    w(f"* Rows serializable into the original file format: **{bt['rows_serializable']}/{bt['extracted']}**")
    w("")
    w("| Column | Structurally compatible | Blank now / blank in 2020 | Same value | Notes |")
    w("|---|---|---|---|---|")
    for c in schema.COLUMNS:
        s = bt["columns"][c]
        w(f"| `{c}` | {s['compatible']}/{s['n']} | {s['blank_now']}/{s['blank_then']} | {s['equal']}/{s['n']} | {s['note']} |")
    w("")
    w("\"Same value\" is informational only. A difference means Steam's data changed, not that the format is "
      "incompatible.")
    w("")
    ev = bt.get("price_rule_evidence")
    if ev:
        w("**Price conversion check.** For back-tested games with a current regular price, which cents-to-dollars "
          "rule reproduces the 2020 value?")
        w("")
        w("| Rule | Games |")
        w("|---|---|")
        w(f"| Only round half up (the documented rule) | {ev['round_only']} |")
        w(f"| Only truncation | {ev['floor_only']} |")
        w(f"| Both rules give the same value | {ev['both']} |")
        w(f"| Neither (the price changed) | {ev['neither']} |")
        w("")
        if ev["round_only"] > ev["floor_only"]:
            w("Interpretation: a game that matches only rounding (for example 1999 cents, recorded as 20 in 2020) "
              "would need a price drop of a few cents to fit truncation. A game that matches only truncation "
              "(for example 999 cents, recorded as 9) also fits rounding plus a $1 price increase since 2020. "
              "The evidence therefore favours the documented round-half-up rule. It cannot prove the rule, "
              "because 2020 prices are not observable.")
        else:
            w("Interpretation: this sample does not favour the documented round-half-up rule; review "
              "`docs/legacy_schema.md` §3.2.")
        w("")

# Integrated crawler

```sh
export STEAM_API_KEY=...                      # needed for GetAppList and GetAppReviews
python -m crawler audit                       # read-only checks
python -m crawler preflight --live            # <= 8 requests (add --steamspy for one more)
python -m crawler run --limit 30              # discover -> crawl -> export -> validate
python -m crawler run --resume                # continue after an interruption, rate limit or --limit
```

The code is in [`crawler/pipeline.py`](../crawler/pipeline.py), with the
command line in [`crawler/cli.py`](../crawler/cli.py). Module-level details:
[discovery.md](discovery.md), [metadata.md](metadata.md),
[reviews.md](reviews.md), [steam_api.md](steam_api.md).

## Commands

| Command | Network | Writes | What it does |
|---|---|---|---|
| `audit` | no | no | Checks the legacy CSV (checksum, 23,034 rows, contract), credential hygiene (key format, the hard-coded key in `steamreviewcrawler.py`), state integrity, stage status and interrupted writes |
| `preflight` | only with `--live` | no | Configuration and live endpoint checks (`docs/steam_api.md`) |
| `discover` | yes | `work/` | Catalog (`GetAppList`, key required) or **seed AppIDs** (`--appid`, `--appids-file`; no key needed), then metadata-based eligibility |
| `crawl` | yes | `work/` | Store metadata and review summaries for the eligible games |
| `export` | no | outputs | Writes the final files from `work/` state |
| `validate` | no | no | Re-checks every output (see below) |
| `run` | yes | both | `discover` → `crawl` → `export` → `validate` |

`extract` and `reviews` remain as lower-level tools working on the same
`work/` files.

### Options

| Option | Commands | Meaning |
|---|---|---|
| `--start-date`, `--end-date` | discover, run | Release-date window. The defaults are 2020-01-01 and today. `--start`/`--end` are accepted as aliases. |
| `--limit N` | discover, crawl, run | At most N lookups per stage in this invocation. The run stays resumable. |
| `--resume` | discover, crawl, run | Continue an unfinished discovery run. `run --resume` goes straight to the crawl when discovery already finished. |
| `--dry-run` | discover, crawl, export, run | Print the plan. No requests, no writes. On a fresh workspace, not even `work/` is created. |
| `--refresh` | crawl, run | Re-fetch games that were already crawled, bypassing the response caches |
| `--enable-steamspy` | crawl, run | Take tags from SteamSpy (third party, cached 24 h) instead of Steam's user tags |
| `--restart`, `--incremental`, `--recheck-unreleased`, `--page-size` | discover, run | See [discovery.md](discovery.md) |
| `--language`, `--purchase-type` | crawl, run | Review-summary settings (see [reviews.md](reviews.md)) |
| `--base-dir`, `--work-dir`, `--legacy-csv` | all pipeline commands | Locations. Outputs go to the base directory (default: the project root); state goes to `BASE/work`. |

## Outputs

| File | Contents |
|---|---|
| `steamproducts2020_2026_new.csv` | New games only. The header line is byte-identical to the legacy file, there are exactly the 13 columns, and the rows use the legacy serialization (UTF-8, CRLF, minimal quoting). Rows are ordered by AppID. |
| `steamproducts2020_2026_merged.csv` | `steamproducts2020.csv` **byte for byte**, then CRLF, then the new rows. If there are no new games, it is identical to the legacy file. |
| `steam_metadata_provenance.csv` | One row per (new AppID, column): value, status, source, source URL, retrieval time, raw value, note |
| `crawl_manifest.json` | Tool version and git commit; window; discovery run and counts; the legacy checksum; crawl, review and Early Access summaries; sha256 and row count of every output; sources; per-stage status; limitations |
| `work/failed_appids.csv` | `appid, stage, error, detail, attempts, last_attempt_at` for every failed lookup. The stage is `discover`, `crawl:metadata` or `crawl:reviews`. |

These intermediate files are under `work/`:

* `work/discovery/` holds `eligible.csv`, `excluded.csv` and the other
  discovery exports.
* `work/metadata/` holds `prices.csv`, `tag_votes.csv`,
  `early_access.csv` and the per-module `games.csv` and `provenance.csv`.
* `work/reviews/` holds `review_summaries.csv` and its `.meta.json`.

A new row requires two things: discovery marked the AppID eligible under
the latest run's window, and its metadata row was built. An AppID in the
legacy file can never appear, because it is excluded before any request is
made, and `export` and `validate` both check again.

## Reliability

* **Persistent status.** All progress lives in SQLite under `work/`, one
  transaction per catalog page or per game. `work/crawl_status.json`
  records each stage as `running`, then `finished`, `incomplete`, `failed`
  or `interrupted`. `audit` reports it, and the manifest embeds it.
* **Response caching.**
  * `appdetails` responses go to `work/cache/appdetails.sqlite3` for 72 h,
    keeping only the fields the pipeline reads. The crawl reuses the
    discovery fetch, so each eligible game costs one `appdetails` request.
  * SteamSpy responses go to `work/cache/steamspy.sqlite3` for 24 h.
  * A cached value keeps its original `retrieved_at`, and the provenance
    note says it came from the cache.
  * Failures are never cached. `--refresh` bypasses the caches.
* **Atomic writes.** Every output is written to a sibling `.<name>.tmp`,
  fsynced, then renamed. A crash leaves either the old file or the new one,
  never a partial file.
* **Retries.** The transport retries timeouts, connection errors, 429 and
  5xx, with backoff and `Retry-After`. A failed lookup is recorded and
  retried on the next invocation. A persistent 429 stops the invocation
  with exit code 75, leaving the game pending.
* **Safe recovery.** Each writing command first takes an exclusive lock
  on `work/crawl.lock`, so a second crawler is refused. It then deletes
  leftover `.*.tmp` files (only interrupted writes produce them) and runs
  `PRAGMA quick_check` on every state file. A corrupt state file stops the
  command. It is never repaired or deleted automatically.
* **Legacy protection.** `export` refuses to run unless
  `steamproducts2020.csv` matches its known sha256. The pipeline only ever
  reads that file.

## Validation (`validate`)

1. **New CSV.** The header bytes equal the legacy header, the rows pass
   the schema contract, every row has 13 fields, there are no duplicate
   AppIDs and no legacy AppIDs, and every AppID is eligible in the
   discovery state.
2. **Merged CSV.** The legacy bytes are an exact prefix, the parsed legacy
   rows are unchanged, the appended rows equal the new CSV's rows, there
   are no duplicate AppIDs, and the whole file passes the contract.
3. **Provenance.** There is exactly one row per (new AppID, column), and
   each value equals the CSV cell.
4. **Manifest.** Every output hash matches, as do the legacy checksum and
   the count of new games.
5. **Failed AppIDs.** The header is correct, and no AppID is both in the
   new CSV and failed at the metadata stage.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success, or stopped at `--limit` |
| 1 | Error (credentials, corrupt state, failed validation, workspace locked) |
| 2 | Usage or state conflict (for example, an unfinished discovery run without `--resume`) |
| 75 | Temporary failure (rate limit, network); run again with `--resume` |
| 130 | Interrupted; run again with `--resume` |

## Repository note

Your global git setting `core.autocrlf=input` stored the original
`steamproducts2020.csv` in history with LF line endings, so a fresh clone
gets a different file. Its checksum fails, and the byte-exact checks
correctly refuse to run. `.gitattributes` now marks `*.csv -text`. To fix
the stored copy, re-add and commit it:

```sh
git add --renormalize steamproducts2020.csv docs/samples
```

# Game discovery

```sh
export STEAM_API_KEY=...
python -m crawler discover                    # full run, window 2020-01-01..today
python -m crawler discover --limit 50         # small trial: catalog + 50 appdetails lookups
python -m crawler discover --resume           # continue an unfinished run from its checkpoint
python -m crawler discover --incremental      # only catalog entries modified since the last finished run
python -m crawler discover --end 2024-12-31   # custom window (--start defaults to 2020-01-01)
```

The code is in [`crawler/discovery.py`](../crawler/discovery.py) (runner and
export), [`crawler/eligibility.py`](../crawler/eligibility.py) (rules),
[`crawler/release_dates.py`](../crawler/release_dates.py) (date parsing) and
[`crawler/discovery_state.py`](../crawler/discovery_state.py) (checkpoints).

## Pipeline

1. **Catalog.** `IStoreService/GetAppList/v1` is called with
   `include_games=true` and every other type `false`. Paging follows
   `last_appid` until `have_more_results` is false. The default page size is
   10,000 (`--page-size`, at most 50,000).
   * Each page's apps and the new cursor are committed in one transaction.
   * A cursor that fails to advance is an error, not an endless loop.
2. **Legacy filter.** An AppID present in `steamproducts2020.csv` is excluded
   as `in_legacy_dataset` and **never fetched**.
3. **Metadata.** Every other candidate is looked up with Store `appdetails`
   (`cc=us`, `l=english`), one request per app, newest AppIDs first. This
   order only matters when `--limit` is used; AppID values are never used to
   judge eligibility.
   * Only the fields needed for the decision are stored as "facts": type,
     `steam_appid`, `fullgame`, `release_date.coming_soon`,
     `release_date.date` (raw text) and genres.
4. **Decision.** `eligibility.classify` is a pure function of (facts, window,
   legacy AppIDs). Decisions are recomputed at export time. Changing `--end`
   therefore reclassifies everything with no new requests, apart from the
   catalog pages.

The catalog's game-only filter is a first pass. The metadata check is the
authoritative one, because some apps listed as games turn out to be DLC,
demos or software.

## Release dates: no invented precision

The raw store string is always kept verbatim. It is parsed into an inclusive
interval with an explicit precision:

| Store text | Precision | Interval |
|---|---|---|
| `3 Dec, 2019`, `Dec 3, 2019`, `December 3, 2019`, `2019-12-03` | day | 2019-12-03..2019-12-03 |
| `Dec 2020`, `December, 2020` | month | 2020-12-01..2020-12-31 |
| `Q1 2021`, `2021 Q1` | quarter | 2021-01-01..2021-03-31 |
| `2021` | year | 2021-01-01..2021-12-31 |
| `TBA`, `Coming soon`, `Early 2021`, `Summer 2021`, `03/04/2021`, `31 Feb, 2021`, … | — | none (unparsed) |

How each case is decided:

* **Eligible:** the game is released (`coming_soon` is not true) and the
  **whole** interval lies inside the window.
* **`released_before_window` or `released_after_window`:** the whole interval
  lies outside the window.
* **`release_date_ambiguous`:** the interval crosses a window boundary (for
  example `Oct 2026` when the window ends 2026-10-30), or the text can't be
  parsed.

`eligible.csv` reports `release_date_precision` and the interval. It never
reports a made-up day.

## Reasons

Every app gets exactly one reason. The first matching rule in this order wins:

| Status | Reason | Rule |
|---|---|---|
| excluded | `in_legacy_dataset` | AppID is in `steamproducts2020.csv` (never fetched) |
| excluded | `store_details_unavailable` | appdetails `success: false`: removed, unlisted or region-locked for `cc=us` |
| excluded | `store_appid_mismatch` | appdetails answered for a different `steam_appid`. The detail flags a legacy target. |
| excluded | `app_type_missing` | No `type` field, so the app can't be confirmed to be a game |
| excluded | `dlc` / `demo` / `soundtrack` | `type` is `dlc` / `demo` / `music` |
| excluded | `software` | `type` is `application`/`tool`/`config`, or every non-business-model genre is a software genre (Utilities, Design & Illustration, …) |
| excluded | `non_game_type` | Any other non-`game` type (video, series, episode, mod, hardware, …) |
| excluded | `linked_to_parent_game` | `type` is `game` but there is a `fullgame` parent (demo- or playtest-style app) |
| excluded | `unreleased` | `release_date.coming_soon` is true |
| excluded | `release_date_missing` | Released, but no release-date text |
| excluded | `release_date_ambiguous` | Unparseable date, or the interval crosses a window boundary |
| excluded | `released_before_window` / `released_after_window` | The interval lies entirely outside the window |
| eligible | `released_in_window` | Passed every rule above |
| failed | `fetch_error` | The appdetails request failed after the transport's retries |

`detail` gives the specific evidence, for example `type='dlc'` or
`release_date='Q1 2021' precision=quarter interval=2021-01-01..2021-03-31 window=2020-01-01..2026-10-30`.

## Outputs (`work/discovery/`, set with `--out`)

All outputs are rewritten atomically at the end of every invocation,
including interrupted ones.

| File | Contents |
|---|---|
| `discovered.csv` | Every catalog app, with its current `status` (`eligible`/`excluded`/`failed`/`pending`) and `reason` |
| `eligible.csv` | `appid, name, type, release_date, release_date_precision, release_date_earliest, release_date_latest, reason, detail, fetched_at` |
| `excluded.csv` | `appid, name, reason, detail, type, release_date, fetched_at` |
| `failed.csv` | `appid, name, reason, error, detail, attempts, last_attempt_at` |
| `summary.json` | Run id and status, window, stop reason, counts, counts per reason |

Every discovered AppID appears in at most one of eligible, excluded and
failed. Pending apps appear only in `discovered.csv`.

## Checkpointing, `--limit`, `--resume`, incremental runs

* **State** lives in a SQLite database (`--state`, default
  `work/discovery.sqlite3`, ignored by git). Every catalog page and
  every lookup is its own transaction, so Ctrl-C loses at most the request
  in flight.
* **`--limit N`** caps the number of appdetails lookups in one invocation. The
  catalog is still paged to the end. A limited run stays unfinished, and
  `--resume` continues it, optionally with another `--limit`.
* **Only one run can be unfinished at a time.** Starting a new run while one
  is unfinished is refused. Use `--resume`, or `--restart` to abandon it. An
  abandoned run's facts are kept and reused.
* **`--resume`** keeps the run's original window, even if "today" has
  changed. Passing a different `--start` or `--end` is an error.
* **Refetching.** An app is fetched again only in these cases:
  * it has no facts yet;
  * its catalog `last_modified` has moved past the facts held;
  * it failed in an *earlier* run, so a failure is retried once per run and
    never repeatedly within one run;
  * `--recheck-unreleased` is set and the facts were unreleased, or had no
    exact date, when fetched.
* **Rate limiting.** An HTTP 429 that survives the transport's retries stops
  the run (exit code 75) and leaves the app pending, not failed. Five
  consecutive lookup failures also stop the run, because they suggest an
  outage.
* **`--incremental`** passes `if_modified_since` = the catalog start time of
  the last *finished* run, so only new or changed catalog entries are
  re-paged. A full run is required first.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Finished, or stopped at `--limit` |
| 1 | Credential or authorization error |
| 2 | Usage or state conflict |
| 75 | Temporary failure (network, rate limit); resume later |
| 130 | Interrupted |

## Cost and known limitations

* **Release dates require one appdetails call per candidate.**
  `GetAppList` carries no release date, and AppID ranges are deliberately
  not used. At the default spacing of 1.5 s, that is roughly 1 hour per
  2,400 candidates. A full first pass over the game catalog, minus the
  23,034 legacy AppIDs, therefore takes days. Plan to use `--limit` and
  `--resume`.
* **`cc=us`:** games not sold in the US come back as `success: false`
  (`store_details_unavailable`).
* **The software rule is a heuristic.** A game whose only genre is
  `Education` is treated as software. The reason and genres are recorded so
  such cases can be reviewed.
* **Unreleased apps.** These are re-checked only when their catalog
  `last_modified` changes or `--recheck-unreleased` is given.
* **Not exercised live.** No live discovery run has been made. All
  behaviour above is verified against an emulated Steam service in
  `tests/test_discovery.py`.

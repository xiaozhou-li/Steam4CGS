# Methodology

## 1. Objective

The aim is to extend `steamproducts2020.csv` (23,034 Steam products) with
games released between **2020-01-01** and a configurable end date, which
defaults to the crawl date. The new rows must use the original 13-column
format byte for byte. The original rows are never modified, and no AppID
appears twice. Every value must be traceable to a dated source request, and
nothing is imputed.

## 2. Sources

| Purpose | Endpoint | Auth | Spacing used | Status |
|---|---|---|---|---|
| Catalog | `api.steampowered.com/IStoreService/GetAppList/v1` | Web API key | ≥ 1.0 s | documented |
| Metadata and eligibility | `store.steampowered.com/api/appdetails` (`cc=us`, `l=english`) | none | ≥ 1.5 s | undocumented |
| Early Access flag, tag ids | `api.steampowered.com/IStoreBrowseService/GetItems/v1` | none | ≥ 1.0 s | documented service, little-described fields |
| Tag names | `api.steampowered.com/IStoreService/GetTagList/v1` | none | once per run | |
| Review summary | `api.steampowered.com/IUserReviewsService/GetAppReviews/v1` | key (not publicly listed) | ≥ 1.0 s | verified per response |
| Community tags (optional) | `steamspy.com/api.php?request=appdetails` | none | ≥ 1.1 s | third party |

On spacing: 1 request/s keeps one key under the Web API's 100,000
calls/day. Store `appdetails` has no published limit, so a conservative
spacing is used. SteamSpy documents a limit of 1 request/s.

On keys: the key is read only from `STEAM_API_KEY` and sent only to
`api.steampowered.com`. It is redacted from every error message, log line
and output.

## 3. Pipeline

`discover` → `crawl` → `export` → `validate` (`python -m crawler run`).
Every stage checkpoints to SQLite under `work/` and can be resumed.

1. **Discovery.** Page through `GetAppList` with `include_games=true`
   until `have_more_results` is false.
   * Seed mode (`--appid`, `--appids-file`) replaces this step with an
     explicit AppID list.
   * AppIDs already in the original CSV are excluded **before any
     request**.
2. **Eligibility.** For every other candidate, one `appdetails` request.
   * Exclude DLC, demos, soundtracks, software, other non-game types,
     apps linked to a parent game, unreleased apps and apps without store
     details.
   * The release date is parsed into an interval with a stated precision
     (day, month, quarter or year). An app is eligible only if the whole
     interval lies inside the window. A date that can't be parsed, or an
     interval that crosses a window boundary, is excluded as
     `release_date_ambiguous`. No day is ever invented.
   * AppID ranges are never used.
3. **Crawl.**
   * Metadata comes from `appdetails`, reused from the discovery cache, and
     `GetItems` for each eligible game.
   * Review summaries are collected separately from the verified
     `GetAppReviews` responses.
4. **Export.** The new and merged CSVs, provenance, manifest and failure
   list are written atomically from the stored state.
5. **Validate.** Every output is re-checked against the contract (§6).

The full field-by-field rules are in [data_dictionary.md](data_dictionary.md)
and [metadata.md](metadata.md).

## 4. Normalization decisions

| Decision | Evidence |
|---|---|
| **List fields.** Exact Python `repr` of a `list[str]`. | All 69,054 non-empty list cells in the original round-trip byte for byte (`docs/legacy_schema.md`). |
| **Genre order.** Case-insensitive alphabetical, with `Early Access` last. | Reproduces 22,929 of the 22,997 original genre lists (99.7%). |
| **Spec order.** The original file's label order, which is the same in all 23,034 rows; labels new since 2020 follow in Steam's order. | Derived from the original file by topological sort. |
| **HTML escaping.** `&`, `<`, `>` are escaped in `title`, `genres`, `developer` and `publisher`; tags stay unescaped. | Matches the original file in every row. |
| **Price.** USD regular price in cents, rounded half up to whole dollars; free = 0; sale prices are never used. | The original distribution peaks at rounded values (10 > 9, 20 > 19). In the back-test, 8 games match only rounding and 5 only truncation, and the truncation-only cases also fit a $1 price increase since 2020 (`reports/quality_report.md`). |
| **Release date.** Exact dates become `D Mon, YYYY`; less precise ones are kept verbatim. | Matches the original's 99.7% canonical format and its free-text placeholders. |
| **Sentiment.** `review_score_desc`, used only if the counts are consistent and the label agrees with them (score labels need ≥ 10 reviews; `N user reviews` needs exactly N). | The original vocabulary (9 labels plus `1–9 user reviews`) is Steam's display rule. |
| **Early Access.** `TRUE` from an explicit `is_early_access: true` or the `Early Access` genre; `FALSE` only from an explicit `false`; otherwise unknown. Tags are never used. | Absence is not evidence. The live preflight showed that Steam omits the flag for games that are not in Early Access. |

## 5. Missing-data policy

* **No imputation and no substitution.** Genres never stand in for tags,
  review counts never stand in for labels, the sale price never stands in
  for the regular price, and the absence of Early Access evidence never
  becomes `FALSE`.
* **Every blank cell has a provenance status** explaining it (`missing`,
  `unverified`, `unrecognized`, `source_unavailable` or `not_collected`).
  The quality checks enforce the rule *blank ⇔ status ≠ `present`*.
* **The schema relaxes only where a blank is honest.** `developer`,
  `specs`, `price` and `early_access` may be blank in new rows, while `id`,
  `title`, `app_name` and `release_date` are required. A game whose
  required fields can't be built is recorded in `work/failed_appids.csv`,
  not written with invented values.

## 6. Reliability and validation

**Reliability:**

* Timeouts on every request.
* Retries with exponential backoff and `Retry-After`. A persistent 429
  stops the run (exit 75) and leaves the item pending.
* Per-host request spacing.
* An exclusive workspace lock.
* One transaction per catalog page or game.
* fsync and atomic rename for every output.
* Leftover temp files removed, and `PRAGMA quick_check` run on every state
  file, before each write stage. Corrupt state is refused, never repaired
  silently.
* A response cache that keeps the original retrieval time.

**Output checks** (`python -m crawler validate`, plus `python -m crawler
quality` at field level). The checks cover:

* the exact 13-column header bytes;
* UTF-8, no BOM, CRLF and a byte-exact re-serialization;
* unique AppIDs, none from the original;
* release dates inside the window;
* integer prices recomputed from the recorded regular price in cents;
* canonical lists and the genre order;
* `TRUE`/`FALSE`/blank backed by evidence;
* the blank ⇔ provenance-status rule;
* metascore from 0 to 100;
* the merged file equal to the original bytes plus the new rows;
* manifest hashes;
* an unchanged original CSV (sha256
  `3a7b7bd5b59565440d05c543557607b498c45590ebddc3f3e7a77afe9d095be8`).

**Test suite.** 777 tests, offline, with sockets blocked. They use mocked
Steam responses through a fake `requests` adapter.

| Area | Files (tests) |
|---|---|
| Contract and original file | `test_schema` (142), `test_legacy_csv` (7) |
| Field mapping | `test_metadata` (96), `test_release_dates` (36), `test_eligibility` (43) |
| Sources | `test_transport` (40), `test_steam_api` (34), `test_reviews` (58), `test_steamspy` (44), `test_credentials` (11), `test_preflight` (10) |
| Stages | `test_discovery` (29), `test_extract` (14), `test_pipeline` (28), `test_backtest` (14) |
| Failure scenarios | `test_resilience` (32): HTTP 5xx, connection errors, 429 (transient, persistent and over-long `Retry-After`), malformed JSON and HTML, wrong shapes, missing fields, and interruptions in the catalog, discovery, crawl, reviews and export. Each scenario must recover to byte-identical outputs and pass every quality check. |
| Output checks | `test_quality` (26): each defect class is planted and must be caught |
| Snapshots | `test_snapshots` (46): derivation, month guards (past, future, crossing midnight), idempotency, cache freshness, retries, 429s, interruption, append-only audit, analysis export dtypes |
| Achievements | `test_achievements` (67): parsing, the unavailable-vs-zero matrix, statistics and thresholds, retries, 429s, interruption, isolation from the 13-column outputs |

Planted bugs (mutation checks) were used to confirm that the key tests fail
when the behaviour they guard is broken.

## 7. Back-testing

A deterministic, stratified sample of **25 games from the original CSV**
(seed 2020) was re-extracted live in an isolated workspace
(`work/backtest/`). The strata are Early Access, free, has a metascore,
fewer than 10 reviews, a free-text release date, and random. The isolated
workspace never feeds the outputs.

* **What it compares.** *Structural compatibility*: is today's cell valid
  under the column's contract, and does it decode to the same Python type
  as the 2020 cell?
* **What it does not compare.** Values. Prices, tags, dates and review
  labels legitimately change; their agreement is reported for information
  only.

Results are in `reports/quality_report.md` and
`reports/backtest_comparison.csv`.

## 8. Pilot and seed mode

Without a Steam Web API key, `GetAppList` and `GetAppReviews` are
unavailable. The pilot ([pilot/README.md](pilot/README.md)) therefore ran
in seed mode on 30 hand-picked AppIDs. Seed mode changes **which AppIDs are
considered**, not how eligibility is decided. Its coverage figures describe
the seeds, not the Steam catalog.

## 9. Limitations and threats to validity

* **Point-in-time data.** Prices, tags, categories and review labels are
  current as of `retrieved_at`, not 2020 values. Games that have left
  Early Access report their 1.0 release date.
* **Undocumented endpoints.** `appdetails` and the `GetItems` fields may
  change. The parsing is defensive, so changes surface as
  `unrecognized`/`source_unavailable`, never as guesses.
* **Region.** `cc=us` excludes games not sold in the US
  (`store_details_unavailable`).
* **Vocabulary drift.**
  * Steam now spells the genre `Free To Play`.
  * About 29% of current spec labels did not exist in 2020 (mostly
    accessibility categories).
  * About 5% of tags are new.
* **Early Access.** Because Steam omits `is_early_access` for games that
  are not in Early Access, `early_access` is mostly unknown rather than
  `FALSE`.
* **Sentiment.** It needs `GetAppReviews` to be visible to the key; if it
  isn't, sentiment stays blank.
* **Franchise.** The original publisher `Franchise:` suffix can't be
  reproduced.
* **Software heuristic.** Games whose only genres are software genres (for
  example `Education`) are excluded as software; the reason is recorded.

## 10. Achievement variables

`python -m crawler achievements` collects achievement data. It uses the
Store summary, the official `ISteamUserStats/GetSchemaForGame/v2` (with a
key) and `GetGlobalAchievementPercentagesForApp/v2`. The results go into
separate files and never into the 13-column data
([achievements.md](achievements.md)).

* **Zero needs explicit evidence.** As with Early Access, absence is never
  zero. `has_steam_achievements` is `FALSE` only when a source explicitly
  reports none, and a 403, a missing field or a failed request leaves it
  blank.
* **Count precedence.** The count prefers the official schema, then the
  percentage list, then the Store summary. The source is recorded, and the
  Store and percentage counts are cross-checked in the coverage report.
* **Rarity, not difficulty.** The unlock-percentage statistics (mean,
  median, minimum, shares below 10% and 1%) describe **observed rarity**
  among owners at `snapshot_utc`. They are not intrinsic difficulty,
  because ownership without play, audience composition, playtime and
  optional or hidden achievements all lower the percentages.

## 11. Longitudinal snapshots

`python -m crawler snapshot` records one observation per game and UTC
collection month ([snapshots.md](snapshots.md)).

* **Point in time only.** Only the current month can be collected; a
  missed month is never back-filled or interpolated. Observations made
  after a month ends are never filed under it.
* **Idempotent and audited.** Re-running within the month is idempotent,
  and every source response is kept in an append-only audit table with its
  URL and time.
* **Official sources.** Review counts come from the verified official
  `GetAppReviews` summary, and concurrent players from the official
  `ISteamUserStats/GetNumberOfCurrentPlayers`.
* **No recent-window count.** No official summary reports one, so
  `recent_review_count` is left blank rather than estimated.
* **Not retention.** Cumulative review counts and their month-to-month
  change are **not** measures of player retention. A single concurrent-player
  reading per month is a noisy point-in-time sample, not activity over the
  month. Both are weak, biased proxies for longevity.

## 12. Reproducibility

* **Inputs.** The original CSV is pinned by its sha256. `export` refuses to
  run if it differs.
* **Workspace.** All state lives in `work/` and all outputs in the project
  root. `crawl_manifest.json` records the tool version, git commit, window,
  settings and output hashes.
* **Line endings.** `.gitattributes` keeps CSV bytes exact (`*.csv -text`).
  The copy of the original CSV stored in git history before this change
  has LF line endings, so re-add it as `docs/pipeline.md` describes.

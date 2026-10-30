# Data dictionary

This document covers every file the crawler produces. The 13-column
datasets keep the exact format of `steamproducts2020.csv`, which
[legacy_schema.md](legacy_schema.md) documents. The extraction rules are in
[metadata.md](metadata.md).

## 1. Game datasets

* `steamproducts2020_2026_new.csv`: new games only.
* `steamproducts2020_2026_merged.csv`: the original file byte for byte,
  followed by the new games.

**Serialization.** Files are UTF-8 without a BOM, with CRLF line endings
and minimal quoting (`csv.QUOTE_MINIMAL`). There is one header row with
the 13 columns in this order. In the new file, every record ends with CRLF.
In the merged file, the original part keeps its original bytes, which have
no final newline, and a CRLF is added before the first appended row.

**Missing values.** A missing value is always the empty cell. The literal
strings `None`, `N/A` and `nan` are data, never markers. Read the files with
`pd.read_csv(path, dtype=str, keep_default_na=False)`.

| # | Column | Type | Representation | Blank means | Source (new rows) |
|---|---|---|---|---|---|
| 1 | `id` | int | Steam AppID, decimal, no leading zeros; unique in each file | never blank | requested AppID; `appdetails.steam_appid` must agree |
| 2 | `title` | text | `app_name` with `&`, `<`, `>` HTML-escaped (`html.escape(…, quote=False)`) | never blank | `appdetails.name` |
| 3 | `genres` | list | Python list repr of str, e.g. `['Action', 'Indie']`. Sorted case-insensitively with `Early Access` last; `&` is HTML-escaped. | store lists no genres | `appdetails.genres[].description` |
| 4 | `developer` | text | Names joined with `", "`. This is not a list, and splitting it is unsafe. | store lists none | `appdetails.developers` |
| 5 | `publisher` | text | Same as developer. Original rows may end in `Franchise:<name>`; new rows never do. | store lists none | `appdetails.publishers` |
| 6 | `release_date` | text | `D Mon, YYYY` (`3 Dec, 2019`; day not zero-padded; English month) when the store gives an exact date, otherwise the store text verbatim (`Q3 2022`, `2022`) | never blank | `appdetails.release_date.date` |
| 7 | `app_name` | text | The display name, unescaped | never blank | `appdetails.name` |
| 8 | `specs` | list | Store category labels (Python list repr) in the original file's fixed label order, then newer labels in Steam's order | store lists no categories | `appdetails.categories[].description` |
| 9 | `tags` | list | Up to 20 user tags (Python list repr), most votes first. `&` is not escaped. | no tags, or the source failed; **genres are never substituted** | GetItems + GetTagList, or SteamSpy with `--enable-steamspy` |
| 10 | `price` | int | Whole USD. `round_half_up(initial_cents / 100)` of the **regular** listing price; `0` = free. | not free and no regular USD price | `appdetails.price_overview.initial` / `is_free` |
| 11 | `sentiment` | text | One of the 9 Steam review labels (`Overwhelmingly Positive` … `Overwhelmingly Negative`), or `N user reviews` for N = 1–9 | no verified review summary, or 0 reviews | `IUserReviewsService/GetAppReviews` (verified) |
| 12 | `early_access` | bool | `TRUE` / `FALSE` | **unknown**: no explicit store flag, conflicting evidence, or a failed request | `GetItems.is_early_access` + the `Early Access` genre |
| 13 | `metascore` | int | 0–100 | no Metacritic score on the store | `appdetails.metacritic.score` |

Cross-field rules, enforced by `crawler/schema.py`:

* `title == html.escape(app_name, quote=False)`.
* A `TRUE`/`FALSE` `early_access` value must agree with the `Early Access`
  genre whenever `genres` is present.
* For new rows, only `id`, `title`, `release_date` and `app_name` are
  required. Original rows have no blanks in `developer`, `specs`, `price` or
  `early_access`; new rows may.

## 2. `steam_metadata_provenance.csv`

One row per (new AppID, column), so 13 rows per game.

| Column | Meaning |
|---|---|
| `appid` | AppID |
| `field` | One of the 13 column names |
| `value` | The exact cell written to the game dataset |
| `status` | See the table below |
| `source` | `<source>:<path>`, e.g. `appdetails:price_overview.initial`, `store_items:tags[].tagid`, `steamspy:tags`, `user_reviews:GetAppReviews.review_score_desc` |
| `source_url` | The exact request, **never including the API key** |
| `retrieved_at` | UTC ISO-8601 time the source was fetched. For cached responses, this is the original fetch time. |
| `raw` | The original JSON of the source field(s), e.g. the whole `price_overview` with currency, regular and sale amounts |
| `note` | The rule applied, the reason for a blank, and whether the value came from the cache |

| `status` | Meaning | Cell |
|---|---|---|
| `present` | The value came from the source | filled |
| `missing` | The source answered but has no value | blank |
| `unverified` | The evidence to decide is missing or conflicting (`early_access`) | blank |
| `unrecognized` | A value exists but doesn't fit the contract (non-USD currency, invalid score, a review label or summary that fails verification) | blank |
| `source_unavailable` | The request for that source failed (the row is retried on the next run) | blank |
| `not_collected` | No review summary collected yet (`sentiment`) | blank |

## 3. `work/failed_appids.csv`

| Column | Meaning |
|---|---|
| `appid` | AppID |
| `stage` | `discover` (the eligibility `appdetails` lookup), `crawl:metadata` or `crawl:reviews` |
| `error` | Exception class or status, e.g. `SteamHTTPError`, `store_details_unavailable`, `normalization_error`, `api_error` |
| `detail` | Message with the key redacted (e.g. `GET store.steampowered.com/api/appdetails -> HTTP 500 …`) |
| `attempts` | Invocations that tried it (blank for review errors) |
| `last_attempt_at` | UTC time of the last attempt |

Rate-limited requests are **not** failures. They stay pending and are
retried with `--resume`.

## 4. `crawl_manifest.json`

| Key | Contents |
|---|---|
| `tool` | Name, version, git commit, Python and `requests` versions |
| `generated_at` | UTC export time |
| `window` | `start`, `end` (inclusive release-date window) |
| `discovery` | Run id, `mode` (`full`, `incremental` or `seed`), status, times, counts per decision status |
| `legacy` | Path, sha256, row count, and `preserved_in_merged` |
| `crawl` | Eligible count, new games, eligible games not crawled yet, tag sources, Early Access status counts |
| `reviews` | Endpoint, status counts, the language and purchase-type settings used, rows with sentiment |
| `outputs` | For each output, its sha256 and row count |
| `sources` | Endpoint URLs |
| `intermediate` | Paths of the files in §5 |
| `stages` | Last state of each stage, from `work/crawl_status.json` |
| `limitations` | Known limitations, as text |

## 5. Supporting datasets under `work/`

| File | Columns |
|---|---|
| `discovery/discovered.csv` | `appid, name, status, reason, last_modified, price_change_number, first_seen_at, last_seen_at` |
| `discovery/eligible.csv` | `appid, name, type, release_date, release_date_precision, release_date_earliest, release_date_latest, reason, detail, fetched_at` |
| `discovery/excluded.csv` | `appid, name, reason, detail, type, release_date, fetched_at`; reason codes are listed in [discovery.md](discovery.md) |
| `discovery/failed.csv` | `appid, name, reason, error, detail, attempts, last_attempt_at` |
| `metadata/prices.csv` | `appid, price, status, currency, initial_cents, final_cents, discount_percent, is_free, source, source_url, retrieved_at, conversion` |
| `metadata/tag_votes.csv` | `appid, rank, tag, count, count_kind (steamspy_votes or steam_tag_weight), source, source_url, retrieved_at` |
| `metadata/early_access.csv` | `appid, early_access, status (true/false/unknown), reason, store_item_is_early_access, genres_early_access, store_item_url, store_item_retrieved_at, appdetails_url, appdetails_retrieved_at` |
| `reviews/review_summaries.csv` | `appid, status, sentiment, review_score_desc, review_score, total_positive, total_negative, total_reviews, review_language, purchase_type, params_sent, params_not_supported, source_url, retrieved_at, detail` |

## 6. Achievement datasets (outside the 13-column format)

These are described fully in [achievements.md](achievements.md).

| File | Grain | Columns |
|---|---|---|
| `steam_achievements_features.csv` | one row per game | `appid, snapshot_utc, has_steam_achievements, achievement_count, achievement_count_source, global_unlock_percentages_available, mean_global_unlock_pct, median_global_unlock_pct, min_global_unlock_pct, share_below_10pct, share_below_1pct` |
| `steam_achievements_per_achievement.csv` | one row per (game, achievement) | `appid, snapshot_utc, achievement_api_name, display_name, hidden, in_schema, global_unlock_pct, pct_source` |
| `work/achievements/achievement_sources.csv` | one row per game | per source: status, count, URL, retrieval time; `conflict`; `detail` |

A blank means unknown; `FALSE`/`0` means a source explicitly reported no
achievements. Unlock percentages measure observed rarity, not difficulty.

## 7. Reports

| File | Contents |
|---|---|
| `reports/quality_report.md` | Validation checks, coverage, missingness, exclusions, failures, back-test and limitations (generated) |
| `reports/backtest_comparison.csv` | `appid, stratum, column, legacy_value, current_value, compatible, equal, current_status, note` (one row per back-tested game and column) |
| `reports/backtest_summary.json` | Aggregates used by the quality report |
| `reports/achievements_coverage.md` | Achievement-data coverage per group, source statuses, count agreement (generated) |

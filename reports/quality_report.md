# Data quality report

Generated 2026-10-30T04:54:13Z by `python -m crawler quality` from the files on disk. Git commit: `cedca8dbc1e08ab2372040b219d7c7cf1caa0fba`.

* **Window:** 2020-01-01 to 2026-10-30
* **Discovery mode:** `seed`
* **New games:** 23
* **Original rows:** 23034

> **Seed mode:** this dataset comes from explicit AppIDs, not the `GetAppList` catalog.
> Coverage below describes those seeds, not the whole Steam catalog.

## Validation checks

| Check | Result | Detail |
|---|---|---|
| original CSV unchanged | PASS | sha256 3a7b7bd5b59565440d05c543557607b498c45590ebddc3f3e7a77afe9d095be8 |
| exact 13-column schema | PASS | header identical to the original, 13 fields per row |
| CSV serialization | PASS | UTF-8 without BOM, CRLF, minimal quoting, round-trips byte-exactly |
| unique AppIDs | PASS | 23 unique new AppIDs, none in the original; 23057 unique in merged |
| release dates within the window | PASS | window 2020-01-01..2026-10-30; precision day=23 |
| integer USD prices from the regular price | PASS | 23 priced, 3 on sale at crawl time (regular price used) |
| list formatting | PASS | canonical Python-list repr; genres in legacy order; <= 20 tags |
| early_access booleans | PASS | TRUE=1, unknown=22; FALSE only from an explicit store flag |
| missing-data handling | PASS | blank <=> provenance status != present; sentiment only from verified summaries; no value substituted from another field |
| metascore integers 0-100 | PASS | 12 present |
| merged = original bytes + new rows | PASS | 7855662 original bytes preserved, 23 rows appended |
| pipeline validate | PASS | manifest hashes, eligibility and failed-AppID checks passed |

## Collection coverage

| Stage | Count |
|---|---|
| AppIDs discovered | 30 |
| Discovery eligible | 23 |
| Discovery excluded | 7 |
| Discovery failed | 0 |
| Discovery pending | 0 |
| Eligible but not crawled yet | 0 |
| New games in `steamproducts2020_2026_new.csv` | 23 |
| Review summaries collected | 0 (none) |
| Rows with sentiment | 0 |

## Missingness per field

A blank cell is always explained by a provenance status. Statuses:

* `missing`: the source has no value.
* `unverified`: there is no explicit evidence.
* `unrecognized`: the value doesn't fit the contract.
* `source_unavailable`: the request failed.
* `not_collected`: the review summary was not collected.

| Column | Filled | Blank | Blank % | Why blank |
|---|---|---|---|---|
| `id` | 23 | 0 | 0.0 | — |
| `title` | 23 | 0 | 0.0 | — |
| `genres` | 23 | 0 | 0.0 | — |
| `developer` | 23 | 0 | 0.0 | — |
| `publisher` | 23 | 0 | 0.0 | — |
| `release_date` | 23 | 0 | 0.0 | — |
| `app_name` | 23 | 0 | 0.0 | — |
| `specs` | 23 | 0 | 0.0 | — |
| `tags` | 23 | 0 | 0.0 | — |
| `price` | 23 | 0 | 0.0 | — |
| `sentiment` | 0 | 23 | 100.0 | not_collected=23 |
| `early_access` | 1 | 22 | 95.7 | unverified=22 |
| `metascore` | 12 | 11 | 47.8 | missing=11 |

## Exclusions

| Reason | Count | Rule |
|---|---|---|
| `in_legacy_dataset` | 6 | AppID already present in steamproducts2020.csv (never fetched) |
| `dlc` | 1 | type is 'dlc' |

## API failures

No AppID failed at any stage (`work/failed_appids.csv` is empty).

## Back-test on games from the original CSV

25 AppIDs from `steamproducts2020.csv` (deterministic stratified sample, seed 2020) were re-extracted with the current pipeline on 2026-10-30T04:48:29Z. This compares **structural compatibility**: whether today's cells use the same representation as the 2020 cells. It does not compare historical values, which legitimately change.

* Still on the store and extracted: **24**
* Not extractable (delisted, region-locked or failed): 1 (263280: store_details_unavailable)
* Rows passing the full 13-column contract: **24/24**
* Rows serializable into the original file format: **24/24**

| Column | Structurally compatible | Blank now / blank in 2020 | Same value | Notes |
|---|---|---|---|---|
| `id` | 24/24 | 0/0 | 24/24 |  |
| `title` | 24/24 | 0/0 | 24/24 |  |
| `genres` | 24/24 | 0/0 | 17/24 | 95% of current labels exist in the 2020 vocabulary |
| `developer` | 24/24 | 0/0 | 20/24 |  |
| `publisher` | 24/24 | 0/0 | 16/24 | 2020 'Franchise:' suffixes are not reproduced |
| `release_date` | 24/24 | 0/0 | 16/24 | 2020 free text=2; date differs=6; same date=16 |
| `app_name` | 24/24 | 0/0 | 24/24 |  |
| `specs` | 24/24 | 0/0 | 0/24 | 71% of current labels exist in the 2020 vocabulary |
| `tags` | 24/24 | 0/0 | 3/24 | 95% of current labels exist in the 2020 vocabulary |
| `price` | 24/24 | 0/0 | 15/24 | median change +0 USD |
| `sentiment` | 24/24 | 24/6 | 6/24 | review summaries unavailable without a key |
| `early_access` | 24/24 | 19/0 | 4/24 | blank = unknown (no explicit store flag) |
| `metascore` | 24/24 | 20/20 | 24/24 |  |

"Same value" is informational only. A difference means Steam's data changed, not that the format is incompatible.

**Price conversion check.** For back-tested games with a current regular price, which cents-to-dollars rule reproduces the 2020 value?

| Rule | Games |
|---|---|
| Only round half up (the documented rule) | 8 |
| Only truncation | 5 |
| Both rules give the same value | 0 |
| Neither (the price changed) | 4 |

Interpretation: a game that matches only rounding (for example 1999 cents, recorded as 20 in 2020) would need a price drop of a few cents to fit truncation. A game that matches only truncation (for example 999 cents, recorded as 9) also fits rounding plus a $1 price increase since 2020. The evidence therefore favours the documented round-half-up rule. It cannot prove the rule, because 2020 prices are not observable.

## Source-specific limitations

**IStoreService/GetAppList/v1**

* Requires a Steam Web API key; without one, discovery runs only in seed mode (explicit AppIDs).
* Carries no release dates, so every candidate needs one appdetails request.

**Store appdetails (undocumented)**

* Unversioned; fields can change or disappear without notice.
* cc=us: apps not sold in the US return success=false (excluded as store_details_unavailable).
* release_date is the current store date (the 1.0 date for games that have left Early Access).
* No franchise field, so the legacy 'Franchise:' publisher suffix is never produced.
* Genre/category vocabulary has drifted since 2020 (e.g. 'Free To Play', accessibility categories).
* Metacritic scores are often absent.

**IStoreBrowseService/GetItems/v1**

* is_early_access is omitted (not sent as false) for games that are not in Early Access, so early_access is TRUE or unknown; FALSE needs an explicit flag.
* Tag weights are Steam's internal ranking, not vote counts.

**IStoreService/GetTagList/v1**

* Tag names are localized; English is requested.

**IUserReviewsService/GetAppReviews/v1**

* Not in the public method list; visible (if at all) only with a key. Without it, sentiment is blank.
* Request parameter names and the response shape are verified per response; failures leave sentiment blank.

**SteamSpy (optional)**

* Third-party estimates; can lag the store by a day or more; documented limit 1 request/second.

## Test suite

```
python -m pytest -q: 664 passed in 112.00s (0:01:52) (exit code 0)
```

## Reproduce

```sh
python3.13 -m pytest                                   # offline test suite (mocked APIs)
python3.13 -m crawler backtest --sample 25             # live back-test on original games
python3.13 -m crawler quality --run-tests              # regenerate this report
```

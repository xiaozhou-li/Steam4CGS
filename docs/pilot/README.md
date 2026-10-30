# 30-game pilot (2026-10-30)

```sh
python -m crawler audit
python -m crawler preflight --live --steamspy
python -m crawler run --appids-file docs/pilot/seed_appids.txt --limit 30 --dry-run
python -m crawler run --appids-file docs/pilot/seed_appids.txt --limit 30
```

**Why seed mode.** `STEAM_API_KEY` was not available, so
`IStoreService/GetAppList` (key required) could not be used for discovery.
The 30 AppIDs in [`seed_appids.txt`](seed_appids.txt) were picked by hand:

* 23 expected new games;
* 1 DLC;
* 6 AppIDs that turned out to be in the legacy CSV.

Eligibility was still decided only from live Store metadata.

## Result

| | |
|---|---|
| Wall time | 64 s |
| Requests | about 50, all keyless: 24 `appdetails`, 1 `GetSupportedAPIList`, 23 `GetItems`, 1 `GetTagList`. Preflight added 5. |
| Discovery | 30 seeds; **23 eligible**, 7 excluded (6 `in_legacy_dataset`, never fetched; 1 `dlc`: Cyberpunk 2077: Phantom Liberty), 0 failed |
| Crawl | 23 complete, 0 partial, 0 failed. All 23 `appdetails` came from the discovery cache, with 0 new requests. |
| Exit code | 0 |
| `validate` | all checks passed |

| Output | Rows |
|---|---|
| `steamproducts2020_2026_new.csv` | 23 |
| `steamproducts2020_2026_merged.csv` | 23,057 (23,034 legacy rows, byte-identical prefix, plus 23) |
| `steam_metadata_provenance.csv` | 299 (23 × 13) |
| `work/failed_appids.csv` | 0 |

The legacy CSV's sha256 was unchanged afterwards.

## Field coverage (23 new games)

| Column | Filled | Notes |
|---|---|---|
| id, title, app_name, genres, developer, publisher, release_date, specs, tags, price | 23/23 | Every game has 20 tags. 3 games were **on sale** during the pilot, and the regular price was used (Resident Evil 4: $9.99 sale, recorded as 40). |
| metascore | 12/23 | The other 11 have no `metacritic` in appdetails |
| sentiment | 0/23 | `GetAppReviews` is not visible without a key, so it stays blank (`not_collected`) |
| early_access | 1/23 | Lethal Company `TRUE` (explicit flag plus genre). The other 22 are unknown. |

## Findings

1. **The `is_early_access: false` flag is never sent.** For non-Early-Access
   games, GetItems omits `is_early_access`. Preflight confirmed this on
   Portal 2, and all 22 such pilot games showed the same. Under the rule
   that absence is never `FALSE`, those games are unknown. Treating "store
   item present, flag absent, no Early Access genre" as `FALSE` would rely
   on Steam's protobuf JSON omitting default values. **That is a decision
   to make, not something the crawler assumes.**
2. **Games that have left Early Access carry their 1.0 date.** Valheim and
   Palworld report 2026 release dates, because Steam's `release_date` is
   the full-release date. The 2020 legacy file recorded the Early Access
   start date for games still in Early Access.
3. **Genre vocabulary has drifted.** Steam now returns `Free To Play` (2
   games), where the legacy file has `Free to Play`. This could be mapped
   to the legacy spelling.
4. **Spec vocabulary has grown.** 24 Store category labels didn't exist in
   2020. Examples are the accessibility categories (`Camera Comfort`,
   `Subtitle Options`, …), `Family Sharing`, and the `DualSense`/`DualShock`
   controller labels. They are kept and flagged in provenance.
5. **New tags.** 13 of the 139 distinct tags (such as `Roguelite` and
   `Extraction Shooter`) are not in the legacy vocabulary.
6. **Date format.** `appdetails` English dates come as `Apr 18, 2011` and are
   normalized to `18 Apr, 2011`.

## Not exercised

* `GetAppList` discovery (needs a key).
* `GetAppReviews` sentiment (needs a key, and visibility to that key).
* SteamSpy tags in a crawl: only one preflight request was made, which
  returned 20 tags with votes.

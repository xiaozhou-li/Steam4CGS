# Achievement-system variables

```sh
python -m crawler achievements --scope new            # games in steamproducts2020_2026_new.csv
python -m crawler achievements --scope legacy         # games in steamproducts2020.csv (23,034; resumable)
python -m crawler achievements                        # both (default: --scope merged)
python -m crawler achievements --appids-file F        # specific AppIDs
python -m crawler achievements --scope legacy --limit 500 --dry-run
```

The code is in [`crawler/achievements.py`](../crawler/achievements.py). The
outputs are separate files. The 13-column metadata datasets are never read
for writing or changed; the tests check that they stay byte-identical.

## Interpretation: rarity, not difficulty

A global unlock percentage is the share of Steam accounts that own the
game and have unlocked the achievement. It measures **observed rarity**,
not **intrinsic difficulty**. Things that lower a percentage without making
an achievement harder include:

* owners who never launched the game, such as bundle, giveaway and
  free-weekend accounts;
* playtime and how far players got;
* achievements that are optional, hidden, time-limited or tied to
  multiplayer;
* achievements added late in a game's life.

Comparisons across games mix all of these with the composition of each
game's audience.

## Sources (per game)

| Source | What it gives | Access |
|---|---|---|
| Store summary: `appdetails.achievements.total` and the `Steam Achievements` category | The count of achievements shown on the store, and whether the store lists the feature | No key; reused from the `appdetails` cache when present |
| `ISteamUserStats/GetSchemaForGame/v2` (`appid`, `l=english`) | The official achievement list: API name, display name, hidden flag | **Steam Web API key**; skipped without one (`not_attempted`) |
| `ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2` (`gameid`) | Global unlock percentage per achievement API name | No key needed |

Every source is classified with one of these statuses, recorded in
`work/achievements/achievement_sources.csv` together with its URL and
retrieval time:

| Status | Meaning |
|---|---|
| `ok` | The source lists achievements |
| `empty` | The source **explicitly** reports none: an empty percentage list, a schema for a known game with no achievements, or a Store total of 0 |
| `no_data` | The source answered without saying either way (for example, `appdetails` has no achievements summary and no category) |
| `not_available` | HTTP 4xx or `success: false`. The source refuses the app. **This is not zero.** |
| `error` | Timeout, 5xx, network error. Retried on the next run. |
| `unrecognized` | The response does not have the documented shape (for example, a percentage outside 0–100). Nothing is derived from it. |
| `not_attempted` | The source needs a key and none was set |

## `steam_achievements_features.csv`

One row per collected game. Booleans are `TRUE`/`FALSE`, and a blank cell
means unknown, as in the 13-column data.

| Column | Definition |
|---|---|
| `appid` | Steam AppID |
| `snapshot_utc` | UTC time of the global-percentages request; if none was made, the schema request, then the Store summary. The percentages change over time. |
| `has_steam_achievements` | `TRUE` if any source shows at least one achievement (including the Store category). `FALSE` only if a source is `empty` and none shows achievements. Otherwise blank (unknown). |
| `achievement_count` | The first positive count in source order: schema, then global percentages, then Store summary. `0` only when `has_steam_achievements` is `FALSE`. Blank when unknown, including when achievements exist but no source counts them. |
| `achievement_count_source` | `schema`, `global_percentages` or `store_summary`; blank when the count is blank |
| `global_unlock_percentages_available` | `TRUE` when valid percentages were returned. `FALSE` when the endpoint returned an empty list or refused the app (4xx). Blank when the request failed or the response was unrecognized. |
| `mean_global_unlock_pct` | Arithmetic mean of the per-achievement percentages (0–100) |
| `median_global_unlock_pct` | Median of the percentages |
| `min_global_unlock_pct` | The rarest achievement's percentage |
| `share_below_10pct` | Fraction (0–1) of achievements with percentage **< 10** |
| `share_below_1pct` | Fraction (0–1) with percentage **< 1** |

The last five columns are filled only when the percentages are available
(`TRUE`). They are computed over every achievement the endpoint returns,
are rounded to at most 4 decimals, and are written in fixed-point form.

**Unavailable is not zero.** For example:

* No achievements summary, no category, and a 403 from the percentages
  endpoint gives `has_steam_achievements` blank and a blank count, not
  `FALSE`/`0`. Lethal Company in the pilot is such a case.
* An empty percentage list gives `FALSE`, with count `0`.

## `steam_achievements_per_achievement.csv`

One row per (game, achievement). The rows follow schema order, followed by
achievements present only in the percentages (in Steam's order).

| Column | Definition |
|---|---|
| `appid` | Steam AppID |
| `snapshot_utc` | As above |
| `achievement_api_name` | The achievement's API name (stable identifier) |
| `display_name` | From the schema (blank without a key) |
| `hidden` | `TRUE`/`FALSE` from the schema; blank when unknown |
| `in_schema` | `TRUE` when listed by the schema, `FALSE` when the schema was read but does not list it, blank when no schema |
| `global_unlock_pct` | Percentage 0–100 (blank if the achievement has none) |
| `pct_source` | `GetGlobalAchievementPercentagesForApp` when a percentage is present |

## Operation

* **State and checkpoints.** One record per game is stored in
  `work/achievements.sqlite3`, committed after each game.
* **Reruns.** Collected games are not fetched again, except in these
  cases:
  * a source had an `error`;
  * `--refresh` is given;
  * a key is now set for a game whose schema was `not_attempted`.
* **Exports.** The output files are rewritten atomically after every
  invocation and include every collected game, across scopes.
* **Rate limits.** A 429 that survives the transport's retries stops the
  run (exit 75); the game stays pending. Per-host spacing is ≥ 1.0 s for
  the Web API and ≥ 1.5 s for the Store.
* **Cost.** Each game costs at most 3 requests: `appdetails` (free when
  cached), the schema (only with a key) and the percentages. The full
  `--scope merged` (about 23,000 games) is a multi-hour, resumable run; use
  `--limit`.
* **Coverage report.** `reports/achievements_coverage.md` is regenerated
  by every invocation.

## Limitations

* Without a key there is no schema. Counts then come from the percentages
  or the Store, and display names and hidden flags are blank.
* The percentages endpoint returns 403 for games without achievements and
  for some games that have them (seen live for 41014, which the Store
  lists with the category). So 403 is recorded as `not_available`, never
  as zero.
* Global percentages are rounded by Steam, and older apps may report them
  as strings. Both forms are accepted; values outside 0–100 are rejected.
* All percentages are snapshots: they drift as more players unlock
  achievements.

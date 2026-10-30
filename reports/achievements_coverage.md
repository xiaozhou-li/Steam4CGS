# Achievement data coverage

Generated 2026-10-30T05:27:28Z by `python -m crawler achievements`.

Steam Web API key: not available, so GetSchemaForGame was skipped.

> **Interpretation.** Global unlock percentages measure *observed rarity*: the share of Steam accounts
> owning the game that unlocked an achievement. They reflect who bought and played the game and for how
> long, and how many owners never launched it. They are **not** a measure of intrinsic difficulty.

## New games (steamproducts2020_2026_new.csv)

Collected 23 of 23 games in this group (0 not collected yet).

| Variable | TRUE | FALSE | blank (unknown) |
|---|---|---|---|
| `has_steam_achievements` | 22 | 0 | 1 |
| `global_unlock_percentages_available` | 22 | 1 | 0 |

| `achievement_count_source` | Games |
|---|---|
| blank | 1 |
| global_percentages | 22 |

Unlock-percentage statistics are present for **22/23** games.
Across them, the median per-game mean unlock rate is 30.22%. The median achievement count is 48.0, and the median share of achievements below 1% is 0.

| Source | ok | empty | no_data | not_available | error | unrecognized | not_attempted |
|---|---|---|---|---|---|---|---|
| Store summary (appdetails) | 22 | 0 | 1 | 0 | 0 | 0 | 0 |
| GetSchemaForGame | 0 | 0 | 0 | 0 | 0 | 0 | 23 |
| GetGlobalAchievementPercentagesForApp | 22 | 0 | 0 | 1 | 0 | 0 | 0 |

Conflicting evidence (one source shows achievements, another explicitly none): none.
Store summary total vs global-percentages count: 22/22 games with both sources agree.

## Original games (steamproducts2020.csv)

Collected 25 of 23034 games in this group (23009 not collected yet).

| Variable | TRUE | FALSE | blank (unknown) |
|---|---|---|---|
| `has_steam_achievements` | 14 | 0 | 11 |
| `global_unlock_percentages_available` | 13 | 12 | 0 |

| `achievement_count_source` | Games |
|---|---|
| blank | 12 |
| global_percentages | 13 |

Unlock-percentage statistics are present for **13/25** games.
Across them, the median per-game mean unlock rate is 34.35%. The median achievement count is 17, and the median share of achievements below 1% is 0.

| Source | ok | empty | no_data | not_available | error | unrecognized | not_attempted |
|---|---|---|---|---|---|---|---|
| Store summary (appdetails) | 13 | 0 | 11 | 1 | 0 | 0 | 0 |
| GetSchemaForGame | 0 | 0 | 0 | 0 | 0 | 0 | 25 |
| GetGlobalAchievementPercentagesForApp | 13 | 0 | 0 | 12 | 0 | 0 | 0 |

Conflicting evidence (one source shows achievements, another explicitly none): none.
Store summary total vs global-percentages count: 12/12 games with both sources agree.

Per-achievement rows in total: 1641.

## Status meanings

* `ok`: the source lists achievements.
* `empty`: the source *explicitly* reports zero.
* `no_data`: the source answered without saying either way.
* `not_available`: the source refused the app (HTTP 4xx or success=false). This is never read as zero.
* `error`: the request failed; it is retried on the next run.
* `unrecognized`: the response had an unexpected shape.
* `not_attempted`: the source needs a key, and none was set.


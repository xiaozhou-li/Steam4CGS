# Monthly engagement snapshots

```sh
python -m crawler snapshot --scope new          # this month's snapshot of the new games
python -m crawler snapshot                      # all games (default --scope merged; resumable, use --limit)
python -m crawler snapshot --scope new --dry-run
python -m crawler snapshot --export-only        # rewrite the exports from stored records; no requests
```

Run it once a month, for example from cron on the 1st:

```
0 6 1 * * cd /path/to/Steam4CGS && python3.13 -m crawler snapshot --scope new
```

The code is in [`crawler/snapshots.py`](../crawler/snapshots.py). Snapshots
are separate files and never touch the 13-column datasets.

## Design

* **One record per game and collection month.** The key is (`appid`,
  `snapshot_month`), where the month is the UTC calendar month of
  collection.
* **No invented history.** Only the **current UTC month** can be collected.
  `--month` set to a past or future month is refused, so a missed month
  stays missing; it is never back-filled or interpolated.
* **Month boundaries.** If midnight at the end of a month passes during a
  run, the run stops with `month_ended`. Any observation made after
  midnight stays in the audit trail with its true time, but is never filed
  under the previous month.
* **Idempotent.** Re-running within the month doesn't fetch complete
  records again; the second run makes 0 requests and produces
  byte-identical exports. Sources that failed (`error`) are fetched again
  on the next run, and the successful sources are reused.
* **Audit trail and cache.** Every source response is appended to
  `work/snapshots.sqlite3` with its URL, UTC time, status, trimmed raw
  JSON, detail and run id. It is exported to
  `work/snapshots/snapshot_audit.csv`, and every invocation is logged in
  `snapshot_runs.csv`. The audit table is the within-month cache.
  `--refresh` re-collects and *adds* observations, never deleting earlier
  ones.
* **The `appdetails` cache.** A cached `appdetails` entry is reused only if
  it was retrieved **in the same month**; otherwise it is fetched again.

## Sources

| Indicator | Source | Access |
|---|---|---|
| `review_total`, `review_positive`, `review_negative`, `review_positive_share` | Official `IUserReviewsService/GetAppReviews/v1` summary, verified as in [reviews.md](reviews.md), with the same `language`/`purchase_type` settings | Needs the method to be visible to the key; otherwise `not_attempted` |
| `current_player_count` | Official `ISteamUserStats/GetNumberOfCurrentPlayers/v1` | No key |
| `age_months`, `price_usd_cents`, `achievement_count` | Store `appdetails` (`release_date`, `price_overview.initial`/`is_free`, `achievements.total`) | No key |
| `early_access_current` | `IStoreBrowseService/GetItems` `is_early_access` plus the `Early Access` genre (same rule as the metadata) | No key |
| `recent_review_count` | **None.** No official summary method reports a recent-window count, and counting individual reviews is out of scope. Always blank (`not_available`). | — |

## `steam_game_monthly_snapshots.csv`

A blank cell means not observed; the reason is in the analysis export's
`*_status` columns. `0` is a real observation, for example 0 concurrent
players or a free game.

| Column | Type | Definition |
|---|---|---|
| `appid` | int | Steam AppID |
| `snapshot_month` | YYYY-MM | UTC collection month |
| `snapshot_utc` | ISO-8601 UTC | The latest retrieval time among the record's sources, always inside `snapshot_month` |
| `age_months` | int | Whole months from the **current** store release date to `snapshot_utc`. Blank for inexact dates (`Q3 2022`) or unreleased games. |
| `review_total` | int | Cumulative number of reviews, under the recorded language and purchase-type settings |
| `review_positive` / `review_negative` | int | Cumulative positive / negative reviews |
| `review_positive_share` | float 0–1 | `review_positive / review_total` (4 decimals); blank when the total is 0 |
| `recent_review_count` | int | Always blank (see above) |
| `current_player_count` | int | Concurrent players **at the moment of the request** |
| `achievement_count` | int | Store achievement total; `0` only when the Store explicitly reports 0 |
| `price_usd_cents` | int | **Regular** US listing price in cents (`price_overview.initial`); `0` = free; sale prices are never used |
| `early_access_current` | TRUE/FALSE | In Early Access at snapshot time. Blank means unknown (no explicit flag). |

## Analysis-ready export: `steam_game_monthly_snapshots_analysis.csv`

This is a long panel with one row per (`appid`, `snapshot_month`), sorted
by game and then month. The dtype for every column is in
`steam_game_monthly_snapshots_analysis.schema.json`.

* **Booleans** are written `true`/`false`, with blank for unknown.
* **Numbers** have no formatting.
* **`dataset`** is `new`, `legacy` or `other`.
* **`snapshot_index`** is the game's 0-based snapshot number.
* **`months_since_previous_snapshot`** gives the calendar months since the
  game's previous snapshot. Gaps are shown, never filled.
* **`review_total_change`** is the net change in the *cumulative*
  `review_total` since the previous snapshot, whatever the gap.
* **`record_complete`** is `false` if a source failed that month.
* **`<indicator>_status`** columns give `ok`, `missing`, `not_available`,
  `unrecognized`, `error` or `not_attempted`.

```python
import json, pandas as pd
cols = json.load(open("steam_game_monthly_snapshots_analysis.schema.json"))["columns"]
dtypes = {k: v["dtype"] for k, v in cols.items() if not v["dtype"].startswith("datetime")}
df = pd.read_csv("steam_game_monthly_snapshots_analysis.csv", dtype=dtypes, keep_default_na=False,
                 na_values={k: [""] for k, t in dtypes.items() if t != "string"}, parse_dates=["snapshot_utc"])
```

## Limitations as proxies for game longevity

These indicators are **proxies**. None of them measures how long players
keep playing.

**Cumulative review counts are not retention.**

* A review is written by a small, self-selected fraction of players. The
  propensity to review varies by genre, price, community and time.
* `review_total` only grows or stays flat. Deletions, bans, purchase-type
  filters and Steam's off-topic review-bomb filtering can move it either
  way.
* **Review totals are not player counts.**
* `review_total_change` counts new reviews net of removals over the gap
  between snapshots. It is **not** the number of new players and **not**
  churn or retention. It responds to sales, updates, controversies and
  review-bomb events.
* `review_positive_share` is a cumulative ratio. It moves slowly and hides
  recent sentiment.

**Concurrent players are a single point-in-time reading.**

* `current_player_count` is the number of players in-game at the second of
  the request. It is not monthly active users, not daily peak, and not
  hours played.
* It depends strongly on the time of day, the weekday, time zones, sales,
  free weekends, updates and events. One reading a month is a very sparse,
  noisy sample. Compare games only with great care.
* It counts only players in the game through Steam. It misses offline
  play and non-Steam versions, and includes idling accounts.

**Other caveats:**

* **`age_months`** uses the release date the store shows *now*. For games
  that left Early Access, that is the 1.0 date: Valheim showed 1 month in
  October 2026 despite its 2021 Early Access launch.
* **`price_usd_cents`** is the US regular price at snapshot time. It
  ignores sales, regional pricing and bundles.
* **`early_access_current`** is blank for most games, because Steam omits
  `is_early_access` for games not in Early Access.
* **Review fields** are blank unless `GetAppReviews` is visible to the key.
* **Survivorship.** Delisted games stop appearing
  (`appdetails success=false`). Missing months can't be recovered.
* **Changing definitions.** Store fields change without notice. The audit
  trail keeps the raw values so changes can be detected.

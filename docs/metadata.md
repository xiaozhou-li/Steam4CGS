# Store metadata extraction

```sh
python -m crawler extract                         # every AppID in data/discovery/eligible.csv
python -m crawler extract --limit 25              # checkpointed; run again to continue
python -m crawler extract --appid 620 --appid 730 # specific AppIDs
python -m crawler extract --refresh               # re-fetch AppIDs already extracted
```

The code is in [`crawler/metadata.py`](../crawler/metadata.py) (the pure
field mapping) and [`crawler/extract.py`](../crawler/extract.py) (fetching,
checkpoints and export). AppIDs in `steamproducts2020.csv` are refused.

## Sources (per AppID)

| Source | Request | Used for |
|---|---|---|
| Store `appdetails` (undocumented) | `store.steampowered.com/api/appdetails?appids=<id>&cc=us&l=english` | id, title, app_name, genres, developer, publisher, release_date, specs, price, early_access, metascore |
| `IStoreBrowseService/GetItems/v1` | `api.steampowered.com`, `input_json` with `include_tag_count: 20`, `country_code: US`, `language: english` | tags (ids, most votes first) |
| `IStoreService/GetTagList/v1` | `api.steampowered.com`, `language=english`, **once per invocation** | tag id to name |
| Review-summary records from `python -m crawler reviews` | `IUserReviewsService/GetAppReviews/v1`, collected separately (see [reviews.md](reviews.md)) | sentiment, applied when `games.csv` is exported |

* If `appdetails` fails or returns `success: false`, the app goes to
  `failed.csv` and the remaining requests are skipped.
* `STEAM_API_KEY` is optional here. If it is set, it is sent only to
  `api.steampowered.com`.
* **`extract` never requests reviews.** Every export reads the review
  state (`--reviews-state`, default `data/reviews/state.sqlite3`). Running
  `extract` again after `reviews` refreshes `sentiment` with no new
  requests for apps already extracted.

## Field mapping

| Column | Rule | Blank when |
|---|---|---|
| `id` | Requested AppID. A different `steam_appid` aborts the row (failed). | never |
| `app_name` | `name`, trimmed and HTML-unescaped | never: no name means a failed row |
| `title` | `html.escape(app_name, quote=False)`. Only `&`, `<`, `>` are escaped, as in the legacy file. | never |
| `genres` | `genres[].description`, HTML-escaped like the legacy file (`Animation &amp; Modeling`), sorted case-insensitively, with `Early Access` last. This rule reproduces 22,929 of the 22,997 legacy lists (99.7%). | no genres |
| `developer` / `publisher` | `developers` / `publishers`, trimmed, deduplicated, HTML-escaped and joined with `", "`. `appdetails` has no franchise, so the legacy `Franchise:` suffix is never produced. | empty list or `[""]` |
| `release_date` | An exact date (`Mar 14, 2023`, `14 March, 2023`, `2023-03-14`, …) becomes `14 Mar, 2023`. Anything less precise (`Q3 2022`, `Dec 2021`, `2022`) or unparsed text is kept **verbatim**; no day is invented. | never: no date means a failed row |
| `specs` | Store categories: `categories[].description`. Labels known from the legacy file use its fixed order, which is consistent across all 23,034 rows (`SPEC_ORDER`). Other labels follow, in Steam's order, and are named in the provenance note. `Steam Achievements` appears exactly when Steam lists that category. `Single-player` is not added. | no categories |
| `tags` | User tags: GetItems tag ids sorted by weight, then names from GetTagList, at most 20. Unknown ids are skipped, and the note lists them. **Genres are never substituted.** | no store item, no tags, or a source failed |
| `price` | See [Price](#price). | not free and no usable regular USD price |
| `sentiment` | `review_score_desc` from a **verified** `GetAppReviews` record (`status=ok`; see [reviews.md](reviews.md)), used verbatim. It is never derived from scores or counts. | not collected yet, no reviews, missing or unrecognized label, a response that failed verification, or an API error |
| `early_access` | `TRUE` if `Early Access` is among the genres, `FALSE` if genres are present without it. User tags are not used. | `unverified`: no genres |
| `metascore` | `metacritic.score` if it is an int from 0 to 100 | no `metacritic`, or an invalid score |

Every row is validated by `crawler.schema` before it is stored.
`games.csv` is written with the legacy serialization: UTF-8, CRLF line
endings and minimal quoting.

### Price

`price = round_half_up(price_overview.initial / 100)` in USD, which is the
rounding inferred from the legacy file (`docs/legacy_schema.md` §3.2).

* `initial` is the **regular listing price**. During a sale, `final` and
  `discount_percent > 0` describe the sale price. That is recorded in
  provenance but never used.
* If `initial` is absent, `final` is used only when `discount_percent` is 0
  (not on sale). Otherwise the price is blank.
* `is_free: true` gives `0`.
* Any currency other than `USD` is left blank, not converted. Requests use
  `cc=us`.
* No `price_overview` and not free (not purchasable, or the price is hidden)
  gives a blank.

## Outputs (`data/metadata/`, set with `--out`)

| File | Contents |
|---|---|
| `games.csv` | The 13 legacy columns, one row per extracted AppID, ordered by AppID |
| `provenance.csv` | One row per (AppID, column): `appid, field, value, status, source, source_url, retrieved_at, raw, note` |
| `prices.csv` | Price provenance with explicit columns: `currency, initial_cents, final_cents, discount_percent, is_free, source, source_url, retrieved_at, conversion` |
| `failed.csv` | `appid, error, detail, attempts, last_attempt_at`. The error is one of `store_details_unavailable`, `appdetails_error` or `normalization_error`. |

`provenance.status` is one of:

| Status | Meaning |
|---|---|
| `present` | The value came from the source |
| `missing` | The source answered but has no value |
| `unverified` | The evidence to decide is missing (only `early_access`) |
| `unrecognized` | A value exists but doesn't fit the contract (unknown sentiment label, non-USD currency, invalid score) |
| `source_unavailable` | The request for that source failed |
| `not_collected` | No review summary collected yet (only `sentiment`) |

More about provenance fields:

* `raw` is the original JSON for that field, for example the whole
  `price_overview` with both amounts and the currency.
* `source_url` is the exact request, which never includes the API key.
* `retrieved_at` is the UTC time that source was fetched.

## Checkpointing and retries

Each AppID's row and provenance are committed to SQLite (`--state`, default
`data/metadata/state.sqlite3`). Later invocations process AppIDs in this
order:

1. AppIDs never attempted;
2. AppIDs that failed;
3. *partial* AppIDs, where a source was unavailable.

Complete AppIDs are fetched again only with `--refresh`. An HTTP 429 that
survives retries stops the invocation (exit 75) without marking the app.
At the default request spacing, each app costs about 2.5 s: 1 Store
request at 1.5 s and 1 Web API request at 1 s. Review summaries add
about 1 s per game, in `python -m crawler reviews`.

## Known differences from the legacy file

* **No `Franchise:` suffix in `publisher`.** `appdetails` doesn't expose
  franchises.
* **`specs` vocabulary.** It includes Store categories the 2020 store page
  didn't show, such as `Co-op`, `PvP` and `Family Sharing`. They are
  listed in each provenance note as "not in legacy vocabulary". The
  scraper notices `Profile Features Limited` and `Steam is learning about
  this game` are not categories, so they never appear.
* **`tags` never repeats `Early Access`.** That repetition was a scraping
  artifact.
* **Blanks for unknown values.** `developer`, `specs`, `price` and
  `early_access` can be blank, where the legacy file always had a value.
  This is permitted by the updated schema, and the reason is recorded in
  provenance.

## Not verified live

No live extraction has been run. The response shapes parsed for GetItems
(`store_items[].tags[].{tagid, weight}` / `tagids`) and GetTagList (`tags[].{tagid, name}`) follow those endpoints as publicly
described. To check them, run `python -m crawler preflight --live`
(at most 8 requests). Parsing is defensive: an unexpected shape is
recorded as `source_unavailable` for that field, not guessed.

## Sample output

[`docs/samples/`](samples/) holds the output for four **synthetic** apps
(AppIDs 9900001–9900005, names starting with "Sample"; not real Steam data).
It is generated from the fixtures in
[`tests/sample_data.py`](../tests/sample_data.py) with
`python3.13 tests/sample_data.py`, and `tests/test_extract.py` fails if the
committed files go stale. The four apps cover:

* a sale price;
* a free Early Access game with 3 reviews;
* a sparse record with blanks and `unverified`;
* a failed tags request, plus a review response that fails verification
  (a score label with 9 reviews), so `sentiment` is blank;
* one `success: false` app, which goes to `failed.sample.csv`. Its review
  request also fails, which gives `api_error`.

`review_summaries.sample.csv` and `review_summaries.meta.sample.json` show
the review collector's output for the same apps.

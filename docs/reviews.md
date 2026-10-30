# Review summaries

```sh
export STEAM_API_KEY=...
python -m crawler reviews                          # every AppID in data/discovery/eligible.csv
python -m crawler reviews --limit 100              # checkpointed; run again to continue
python -m crawler reviews --appid 620              # specific AppIDs
python -m crawler reviews --language english --purchase-type steam
python -m crawler extract                          # re-export games.csv with the new sentiment values
```

The code is in [`crawler/reviews.py`](../crawler/reviews.py). The only
source is `IUserReviewsService/GetAppReviews/v1` on `api.steampowered.com`.
Nothing else ever fills `sentiment`: not the Store `/appreviews` page, not
review counts, and not tags.

## Requests

1. **Capability check, once per invocation.** The collector reads
   `ISteamWebAPIUtil/GetSupportedAPIList/v1` with the key, if one is set.
   * If `IUserReviewsService/GetAppReviews/v1` is **not listed**, the
     command stops before sending any review request (exit code 1). It
     does not fall back to another endpoint.
   * The method's declared parameter names are saved to
     `review_summaries.meta.json`.
2. **One summary request per game**, never paged. Of the requested settings
   below, only those the method declares are sent. Any that aren't declared
   are recorded in `params_not_supported`.

| Setting | Requested | Purpose |
|---|---|---|
| `language` | `all` (`--language`) | Review language filter |
| `purchase_type` | `all` (`--purchase-type`: `all`, `steam`, `non_steam_purchase`) | Purchase-type filter |
| `num_per_page` | `0` | Summary only. Individual reviews are never fetched. If the response contains some anyway, they are ignored and not stored. |

`appid` is always sent. The key is sent when it is configured and is never
recorded. Changing `--language` or `--purchase-type` causes games collected
with other settings to be collected again.

## Verification

A response becomes `status=ok`, and its label becomes the legacy
`sentiment` cell, only if all of the following hold:

1. The summary object is found as `response.query_summary`, or as the
   flat `response` object if that already holds the summary fields.
2. `total_positive`, `total_negative` and `total_reviews` are
   non-negative ints, and positive + negative = total.
3. `review_score_desc` agrees with the counts:

| Label | Required counts |
|---|---|
| One of the 9 legacy labels (`Overwhelmingly Positive` … `Overwhelmingly Negative`) | `total_reviews >= 10` |
| `N user reviews` | `total_reviews == N`, with 1 ≤ N ≤ 9 |
| `No user reviews`, or no label | `total_reviews == 0` gives `no_reviews` (sentiment blank) |

| Status | Sentiment | Meaning |
|---|---|---|
| `ok` | the label, verbatim | Every check passed |
| `no_reviews` | blank | Verified 0 reviews |
| `missing_label` | blank | Counts verified (> 0), but no `review_score_desc` |
| `unrecognized_label` | blank | Counts verified, but the label is outside the legacy vocabulary (for example a localized label) |
| `invalid_response` | blank | No summary, bad counts, or a label that contradicts the counts. `detail` says which. |
| `api_error` | blank | The request failed after the transport's retries. It is retried on the next invocation. |

**Rate limits.** An HTTP 429 that survives the transport's retries stops the
invocation (exit code 75) and leaves the game pending, not failed. A 401 or
403 stops the run as an authorization error.

## Outputs (`data/reviews/`)

**`review_summaries.csv`** holds the numeric statistics, separate from
`games.csv`. Its columns are `appid, status, sentiment, review_score_desc,
review_score, total_positive, total_negative, total_reviews,
review_language, purchase_type, params_sent, params_not_supported,
source_url, retrieved_at, detail`.

* `review_score_desc` is the raw label even when it was rejected.
* `sentiment` is the value used in `games.csv`.
* `retrieved_at` is the UTC time of that game's request.

**`review_summaries.meta.json`** records the endpoint, the requested
settings and parameters, the parameter names the method declares, its HTTP
method, and counts per status.

**In `games.csv`,** `extract` copies `sentiment` from these records when it
exports. In `provenance.csv`, the `sentiment` row for each game carries the
review record's status, source URL, retrieval time, raw counts and
settings.

## Access limitations

* `GetAppReviews` is **not in the public method list**. Whether a regular
  (non-publisher) Web API key can see it has not been confirmed. The
  capability check answers this on the first real run. `python -m crawler
  preflight --live` also shows it, together with the verification result
  for one game.
* The request parameter names (`language`, `purchase_type`,
  `num_per_page`) and the response shape (`query_summary` or flat) mirror
  the Store review endpoint. Neither is verified, which is why parameters
  are filtered against the declared list and every response is verified.
  If Steam uses different names, the records show it: settings appear in
  `params_not_supported`, and responses come back as `invalid_response`,
  with no invented sentiment.
* The 10-review threshold for score labels is Steam's documented display
  rule. A game where Steam's label is computed on filtered counts (for
  example after excluding off-topic review bombs) could fail check 3 and
  come back as `invalid_response`. That is the intended safe outcome.

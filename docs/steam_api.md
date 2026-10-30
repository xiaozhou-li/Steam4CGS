# Steam API clients

All network access goes through [`crawler/transport.py`](../crawler/transport.py).
The endpoint clients live in [`crawler/steam_api.py`](../crawler/steam_api.py).

| Purpose | Endpoint | Host | Key | Client method |
|---|---|---|---|---|
| Game discovery | `IStoreService/GetAppList/v1` | `api.steampowered.com` | required | `SteamWebAPI.get_app_list`, `iter_app_list` |
| Review summaries | `IUserReviewsService/GetAppReviews/v1` | `api.steampowered.com` | sent if set | `SteamWebAPI.get_app_reviews` |
| Detailed metadata | `/api/appdetails` (undocumented) | `store.steampowered.com` | **never sent** | `SteamStoreAPI.get_app_details` |
| Capability check | `ISteamWebAPIUtil/GetSupportedAPIList/v1` | `api.steampowered.com` | sent if set | `SteamWebAPI.get_supported_methods` |

`api.steampowered.com` is the public host for regular Web API keys. The
publisher host, `partner.steam-api.com`, is not used, and the transport
rejects any host outside the allowlist.

## Credentials

* The key is read only from the `STEAM_API_KEY` environment variable and must
  be 32 hex characters. Export it in your shell session. Do not pass it as a
  command-line argument, because arguments are visible in `ps` and in shell
  history.
* `ApiKey` objects print as `ApiKey('***')` and cannot be pickled.
* The Web API takes the key as a `key=` query parameter. To keep it from
  leaking:
  * The transport never follows redirects.
  * Every error message has registered secrets and any `key=` value replaced
    with `***`.
  * The original `requests` exception is not chained, because its message
    contains the full URL.
  * A logging filter scrubs `key=` from urllib3's DEBUG log.
* `steamreviewcrawler.py` contains a hard-coded key. Nothing in `crawler/`
  imports or reads that script, apart from preflight, which reports only the
  line numbers. Revoke that key at <https://steamcommunity.com/dev/apikey>,
  issue a new one, and keep the script out of version control until the
  literal is removed.

## Reliability

| Mechanism | Default |
|---|---|
| Timeout | 5 s connect, 30 s read |
| Retries | Up to 4 attempts on connection errors, timeouts, 429 and 500/502/503/504 |
| Backoff | Exponential (1, 2, 4 … s, capped at 60 s) with jitter |
| `Retry-After` | Honoured, in seconds or as an HTTP date. If the server asks for more than 300 s, the client raises instead of waiting. |
| Rate limiting | Minimum interval per host: 1.0 s for `api.steampowered.com`, 1.5 s for `store.steampowered.com`. Thread-safe. A 429 or 5xx pushes back every caller using that host. |
| Request budget | Optional `max_requests`, counting retries. Preflight uses 9. |
| Not retried | 401/403 (raised as `SteamAuthError` on keyed calls), other 4xx, 3xx, non-JSON bodies |

The error hierarchy is `SteamError`, with these subclasses:

* `SteamTransportError`
* `SteamHTTPError`, which has `SteamAuthError` and `SteamRateLimitError`
* `SteamResponseError`
* `RequestBudgetExceeded`

The rate defaults are conservative assumptions, not published guarantees.
1 request/s keeps one key under the Steam Web API Terms of Use limit of
100,000 calls per day. `appdetails` has no published limit. It is commonly
reported to throttle at around 200 requests per 5 minutes, either with HTTP
429 or with a `null` body. The client raises `SteamResponseError` for a
`null` body.

## Endpoint notes

### `IStoreService/GetAppList/v1`

* Returns apps ordered by `appid`, at most 50,000 per page.
* Pagination: pass the previous page's `last_appid` while
  `have_more_results` is true. `iter_app_list` does this and raises if the
  cursor stops advancing.
* Defaults are `include_games=true` and every other type `false`.
  `if_modified_since` (a Unix timestamp) allows incremental discovery.

### `IUserReviewsService/GetAppReviews/v1`

* The client sends `appid` plus any extra parameters unchanged, and returns
  the raw `response` object. It does not interpret the fields yet, because
  the method's parameters and response shape could not be confirmed offline.
* `python -m crawler preflight --live` prints the parameter list Steam
  declares for this method, once a key that can see it is configured.

### Store `appdetails`

* Undocumented and unversioned. Treat every field in `data` as optional and
  expect changes without notice.
* Requests one app per call: `appids=<id>&cc=us&l=english`. US region and
  English match the legacy CSV's USD prices and English genre and category
  names.
* `success: false` (an unknown, removed or region-locked app) returns `None`.

## Preflight

```sh
python -m crawler preflight          # offline: Python, requests, STEAM_API_KEY format, legacy-key warning
python -m crawler preflight --live   # plus at most 9 requests, one app id (default 620)
```

Live checks run only with `--live`. They make one capability check, one
3-app `GetAppList` page, and one `GetAppReviews` and one `appdetails` call
for a single app. A keyed check is skipped when no key is set. The exit code
is 1 if any check fails.

## Access limitations (live run, 2026-10-30)

These results come from a keyless `--live` run, which sent 2 requests:

* **`appdetails` works without a key.** It returned 37 fields for app 620.
* **`GetSupportedAPIList` without a key lists 63 public methods.** Neither
  `IStoreService/GetAppList/v1` nor `IUserReviewsService/GetAppReviews/v1` is
  among them.
  * GetAppList is documented as key-only, so this is expected.
  * GetAppReviews is therefore **not public**. Whether a regular (non-partner)
    Web API key can see it is still **unverified**. Run
    `preflight --live` with `STEAM_API_KEY` set to find out.
  * If the method isn't visible to a regular key, the documented public
    alternative is `store.steampowered.com/appreviews/<appid>?json=1&num_per_page=0`.
    Its `query_summary.review_score_desc` is the same text as the legacy
    `sentiment` column. This is what `steamreviewcrawler.py` used.
* **The keyed endpoints were not exercised.** `STEAM_API_KEY` was not set, and
  the key in `steamreviewcrawler.py` was deliberately not used.

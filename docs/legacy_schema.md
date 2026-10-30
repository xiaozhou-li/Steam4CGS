# Legacy schema: `steamproducts2020.csv`

This document describes the original Steam product file exactly as it exists
on disk. It is the reference contract for any new metadata the crawler
collects. The executable version of the contract is
[`crawler/schema.py`](../crawler/schema.py). The tests in
[`tests/test_legacy_csv.py`](../tests/test_legacy_csv.py) check that every
statement marked **enforced** below holds for all rows of the original file.

The original file is read-only input. Do not edit, re-save or re-export it.

## 1. File-level format

| Property | Value |
|---|---|
| SHA-256 | `3a7b7bd5b59565440d05c543557607b498c45590ebddc3f3e7a77afe9d095be8` |
| Size | 7,855,662 bytes |
| Encoding | UTF-8, **no BOM** (45,901 non-ASCII bytes, all valid UTF-8) |
| Line terminator | CRLF (`\r\n`) on every line. No bare LF or CR. |
| Final newline | **None**. The last record is not followed by CRLF. |
| Header | One row: `id,title,genres,developer,publisher,release_date,app_name,specs,tags,price,sentiment,early_access,metascore` |
| Data rows | 23,034. Every row has exactly 13 fields. |
| Quoting | RFC 4180 minimal quoting. A field is wrapped in `"` only when it contains `,`, `"` or a line break, and an embedded `"` is doubled. |
| Embedded line breaks | None in any cell |
| Row order | Not sorted by `id` or by any other column |

Python's `csv.writer(f, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)`
reproduces the file byte for byte, apart from the missing final CRLF. New files
written by `crawler.schema.write_rows` end with a CRLF, which every CSV reader
accepts.

### Reading safely

Read every cell as a raw string. Two common shortcuts change the data without
warning:

* `pandas.read_csv` with its defaults turns the literal publisher names `None`
  (13 rows), `N/A` (10) and `n/a` (1), and the developer name `N/A` (1), into
  `NaN`. It also reads `metascore` as `float64` and `early_access` as `bool`.
  Use `pd.read_csv(path, dtype=str, keep_default_na=False)`, which yields
  exactly the raw cells.
* Opening the file without `newline=""` can mishandle CRLF line endings inside
  the `csv` module.

## 2. Column contract

The thirteen columns, in this exact order (**enforced**):

| # | Column | Type and representation | Empty cells | Required |
|---|---|---|---|---|
| 1 | `id` | Steam app id. Positive decimal integer, no leading zeros. Unique. Range 50–1,206,470. | 0 | yes |
| 2 | `title` | Display name, **HTML-escaped**. See §3.6. | 0 | yes |
| 3 | `genres` | List of strings (§3.1) | 37 | no |
| 4 | `developer` | Free-text string. Several studios are joined with `", "`. | 0 | yes |
| 5 | `publisher` | Free-text string with an optional `Franchise:` suffix (§3.7) | 5 | no |
| 6 | `release_date` | `D Mon, YYYY` or a free-text placeholder (§3.3) | 0 | yes |
| 7 | `app_name` | Display name, **not** escaped | 0 | yes |
| 8 | `specs` | List of strings (§3.1). Store "features" such as `Single-player` and `Steam Cloud`. | 0 | yes |
| 9 | `tags` | List of strings (§3.1). User tags in Steam's display order. | 11 | no |
| 10 | `price` | Non-negative integer, whole currency units (§3.2) | 0 | yes |
| 11 | `sentiment` | Review-summary label (§3.4) | 1,722 | no |
| 12 | `early_access` | `TRUE` / `FALSE` (§3.5) | 0 | yes |
| 13 | `metascore` | Integer 0–100. Observed range 20–96. | 20,665 | no |

A missing value is always the **empty cell** `""`. The file never uses `NaN`,
`null`, `None` or `[]` to mean "missing". Where those strings appear (for
example a publisher literally named `None`), they are real data.

"Required" reflects what the legacy file shows: no empty cell in all 23,034
rows. For new data, `crawler/schema.py` (`REQUIRED_COLUMNS`) requires only
`id`, `title`, `release_date` and `app_name`. `developer`, `specs`, `price`
and `early_access` may be blank when the store does not provide them, rather
than holding an invented value. A blank `early_access` means unknown. A
`TRUE`/`FALSE` value must still agree with `genres` whenever `genres` is
present. The missingness is recorded in the provenance dataset (see
`docs/metadata.md`).

## 3. Representations

### 3.1 List fields: `genres`, `specs`, `tags`

* **Encoding.** The cell is exactly the Python `repr()` of a `list[str]`, for
  example `['Action', 'Adventure']`. Every non-empty list cell in the legacy
  file round-trips byte for byte through `repr(ast.literal_eval(cell))`
  (**enforced**: non-canonical spellings such as `["Action"]` or
  `['Action','Indie']` are rejected).
  * Elements are single-quoted, separated by `", "`.
  * An element that contains `'` is double-quoted, as `repr` does:
    `"Beat 'em up"`, `"1990's"`. 1,170 `tags` cells contain one.
  * Non-printable characters are backslash-escaped. The only case in the
    legacy file is U+00A0 (no-break space), written as the four characters
    `\xa0`.
  * All 69,054 non-empty list cells are pure ASCII, so the `repr` output does not depend
    on the Python version's Unicode tables.
  * Because the whole list contains `,`, the CSV layer wraps it in `"…"` when
    there are two or more elements. Inner `"` characters are doubled.
* **Missing.** An empty cell means no list. `[]` never appears and is rejected
  (**enforced**). `encode_list([])` returns `""`.
* **Decoding.** Use `ast.literal_eval`, never `eval`.

| Field | Elements per list | Distinct values | Empty-string elements | Duplicates |
|---|---|---|---|---|
| `genres` | 1–16 | 24 | never (**enforced**) | never (**enforced**) |
| `specs` | 1–18 | 31 + `''` | only in the notice pattern below (**enforced**) | never, apart from that pattern (**enforced**) |
| `tags` | 1–20 | 374 | never (**enforced**) | **allowed**: `'Early Access'` appears twice in 2,014 rows |

**`specs` notice pattern.** In 6,709 rows the store page carried one of two
notices, which the old scraper captured with a trailing NBSP followed by two
empty strings:

```
[..., 'Profile Features Limited\xa0', '', '']           # 5,842 rows
[..., 'Steam is learning about this game\xa0', '', '']  # 867 rows
```

The notice is always third from last. The validator accepts empty strings only
in this exact tail. It also accepts a notice with no tail.

**Observed but not enforced.** `specs[0] == 'Single-player'` in every row,
including MMO titles. This is probably an artifact of the original scraper,
not a true per-game fact. `tags` never has more than 20 elements, which is
Steam's on-page limit.

`genres` vocabulary: Indie, Action, Adventure, Casual, Strategy, Simulation,
RPG, Early Access, Free to Play, Sports, Racing, Massively Multiplayer, plus 12
rare software categories (Utilities, Audio Production, `Animation &amp; Modeling`,
and others). Note that `genres` contains one HTML-escaped value while `tags`
contains unescaped `&` (for example `Point & Click`).

### 3.2 Price: `price`

* **Encoding.** A plain decimal integer: no currency symbol, no decimals, no
  leading zeros (**enforced**). Range 0–642. There are 65 distinct values.
* **0.** 1,690 rows. 919 of them have genre `Free to Play`. The rest are free
  titles or demos. The file cannot tell "free" apart from "price unavailable".
* **Currency.** Not recorded in the file. The magnitudes match USD store
  prices, for example Halo MCC is 40, Disco Elysium 40 and Jurassic World
  Evolution 45.
* **Rounding (inferred).** Prices appear to be rounded to the nearest whole
  unit, not truncated. Steam prices end mostly in `.99`, and the distribution
  peaks at the rounded-up values: 1 (2,801), 10 (2,226), 15 (828) and 20 (682)
  are far more common than 0-for-paid, 9 (1,329), 14 (163) and 19 (45). With
  truncation, $0.99 games would show as 0 and $9.99 as 9.
  `crawler.schema.price_from_cents` therefore converts minor units by
  rounding half up (`3999 → 40`, `99 → 1`). This could not be checked against
  the live store, because collection was offline. It is also unknown whether
  the scrape used the base price or a discounted one (Cities: Skylines is 28).

### 3.3 Release date: `release_date`

* **Canonical form**, 22,964 rows (99.7%): `D Mon, YYYY`.
  * The day is **not** zero-padded (`3 Dec, 2019`, never `03`).
  * The month is an English three-letter abbreviation (`Jan`…`Dec`).
  * A comma and a space follow the month.
  * Years range from 1970 to 2023. The 1970 value (app 788510) looks like a
    Unix-epoch placeholder. The 2023 value (app 699570) is a future date
    entered by the developer.
  * Use `crawler.schema.encode_release_date(date)`, not `strftime("%b")`,
    because `%b` depends on the locale.
* **Free-text placeholders**, 70 rows. These are what the developer typed for
  unreleased games, kept verbatim. Examples: `TBA` (4), `2020` (5),
  `Q1 2020` (3), `Oct 2010`, `December 2019`, `When it's done`, `Coming Soon -
  Pre-purchase Available`, `2019年第四季度`, `Dezembro - 2020`.
* **Validation (enforced).**
  * The value must be non-empty with no surrounding whitespace.
  * Anything shaped like `<digits> <word>, <digits>` must be a canonical, real
    calendar date. `03 Dec, 2019`, `3 December, 2019` and `31 Feb, 2019` are
    rejected.
  * Other text is accepted as a placeholder.
  * `parse_release_date` returns `datetime.date` for canonical values and
    `None` for placeholders.

### 3.4 Review sentiment: `sentiment`

Exactly one of the following (**enforced**):

| Value | Rows | Meaning |
|---|---|---|
| `Overwhelmingly Positive` | 313 | Steam review-summary label |
| `Very Positive` | 3,947 | 〃 |
| `Positive` | 2,670 | 〃 |
| `Mostly Positive` | 2,786 | 〃 |
| `Mixed` | 4,020 | 〃 |
| `Mostly Negative` | 747 | 〃 |
| `Negative` | 161 | 〃 |
| `Very Negative` | 28 | 〃 |
| `Overwhelmingly Negative` | 6 | 〃 |
| `N user reviews`, N = 1–9 | 6,634 | Fewer than 10 reviews, so Steam shows a count instead of a label. Always the plural `reviews`, even for `1 user reviews`. |
| *(empty)* | 1,722 | No review summary. Includes 209 Early Access titles. |

This is a categorical string, not a score. The labels are ordered
(`SENTIMENT_LABELS` lists them from most to least favourable), but the
`N user reviews` values do not belong to that scale. `10 user reviews` and
`1 user review` are invalid.

### 3.5 Booleans: `early_access`

* **Encoding.** The uppercase literals `TRUE` and `FALSE` only, as written by
  Excel and pandas (**enforced**). `true`, `True`, `1` and an empty cell are
  rejected. Counts: `FALSE` 20,985, `TRUE` 2,049.
* **Semantics.** `early_access` is `TRUE` **if and only if** `genres` contains
  `'Early Access'`. This holds in all 2,049 Early Access rows (**enforced** as
  a row-level rule). The `tags` column is a weaker signal: 526 non-Early-Access
  games still carry an `Early Access` tag.

### 3.6 `title` and `app_name`

These hold the same name in two encodings:

* `title == html.escape(app_name, quote=False)` in **all** 23,034 rows
  (**enforced**). Only `&`, `<` and `>` are escaped. `title` contains `&amp;`
  208 times and `&gt;` 4 times. Quotes are never escaped.
* They differ in 210 rows, for example
  `Heroes of Might &amp; Magic V` / `Heroes of Might & Magic V`.
* Use `app_name` for display and joins. `title_from_app_name()` derives
  `title`.
* Names are not unique: `app_name` has 23,001 distinct values, so join on `id`.
  32 titles contain double spaces, which are preserved.

### 3.7 `developer` and `publisher`

* Both are **scalar strings**, not lists. Several studios are joined with
  `", "`, for example `343 Industries, Splash Damage, Ruffian Games, Bungie`.
  Splitting on `", "` is **unsafe**, because company names contain the same
  separator: `CAPCOM Co., Ltd.`, `FromSoftware, Inc.`. Do not split.
* Both contain HTML-escaped `&amp;` (developer 63 rows, publisher 62) and
  never an unescaped `&`.
* **Franchise suffix.** In 3,191 rows the old scraper ran the publisher and
  franchise together with no space:
  `Paradox InteractiveFranchise:Cities: Skylines`. The marker appears at most
  once per cell, with non-empty text on both sides (**enforced**). The
  franchise text can itself contain `:` and `, `. Use `split_publisher()` and
  `join_publisher()`. `developer` never carries the marker.
* **Whitespace.** 8 publisher cells have leading or trailing spaces, for
  example `'Mindware Co.,Ltd. '`. These are preserved as-is.
* **Literal "null-looking" names.** `None`, `N/A` and `n/a` appear as
  publisher names, and `N/A` as a developer name. They are data, not missing
  values (see §1, "Reading safely").

### 3.8 `metascore`

A decimal integer from 0 to 100 with no leading zeros, or empty
(**enforced**). Only 2,369 rows (10.3%) have one. It is never written as a
float such as `77.0`, even though pandas loads the column as `float64`.

## 4. Using the contract for new data

* Build a `crawler.schema.GameRecord` with typed values and serialize it with
  `GameRecord.to_row()` or `write_rows(path, records)`. Both validate first and
  raise `SchemaError` listing every violation.
* `write_rows` refuses to overwrite an existing file unless
  `overwrite=True`, which protects the legacy CSV.
* `validate_file(path)` checks encoding, BOM, line endings, header, every
  cell, the row-level rules and `id` uniqueness. It only reads the file.
* The legacy file round-trips exactly: `GameRecord.from_row(row).to_row() ==
  row` for every row, and `write_rows` reproduces the original bytes plus a
  final CRLF.

## 5. Open questions for the crawler phase

These are inferred, not verified, because no network requests were made:

1. Currency and region of `price`, and whether it is the base or the
   discounted price.
2. Which Steam endpoint and field produced `sentiment`. The `N user reviews`
   wording matches the store's review summary. Map "no reviews" to an empty
   cell.
3. Whether to keep emitting the `specs` notice tail (`NOTICE, '', ''`) and the
   always-first `Single-player`. The contract allows both shapes. Choose one
   for consistency with the legacy file.

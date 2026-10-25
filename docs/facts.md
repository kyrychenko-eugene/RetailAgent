# Verified Facts

Facts checked live on **2026-10-25** against `bigquery-public-data.thelook_ecommerce` (jobs run in the author's project, US multi-region) and against official Google documentation. Each fact gives its query or source. Data-dependent numbers change daily (§1.2); later stages must re-measure rather than hard-code them.

Notation: "processed" = bytes processed reported by the dry-run; "billed" = bytes billed (BigQuery bills at least 10 MiB per referenced table, see §2).

---

## 1. thelook tables

### 1.1 Size, partitioning, clustering

```sql
SELECT table_id, row_count, size_bytes, TIMESTAMP_MILLIS(last_modified_time) AS last_modified
FROM `bigquery-public-data.thelook_ecommerce.__TABLES__`;

SELECT table_name, column_name, is_partitioning_column, clustering_ordinal_position
FROM `bigquery-public-data.thelook_ecommerce.INFORMATION_SCHEMA.COLUMNS`;

SELECT table_name, REGEXP_CONTAINS(ddl, r'PARTITION BY'), REGEXP_CONTAINS(ddl, r'CLUSTER BY')
FROM `bigquery-public-data.thelook_ecommerce.INFORMATION_SCHEMA.TABLES`;
```

| Table | Rows | Size (bytes) | Partitioned | Clustered |
|---|---:|---:|---|---|
| `order_items` | 181,777 | 13,638,699 | no | no |
| `orders` | 125,396 | 6,775,058 | no | no |
| `products` | 29,120 | 4,285,975 | no | no |
| `users` | 100,000 | 19,814,736 | no | no |
| `events` (not used) | 2,429,346 | 385,818,885 | no | no |
| `inventory_items` (not used) | 490,412 | 81,346,198 | no | no |
| `distribution_centers` (not used) | 10 | 809 | no | no |

- **No table is partitioned or clustered** (DD §20 assumption confirmed). Every query scans the full referenced columns; date filters do not reduce bytes. The bytes cap is runaway protection only.
- The four tables used total ~44.5 MB, so even a query reading every column of all four stays far below any reasonable cap.

### 1.2 Data refresh and dates

```sql
SELECT 'order_items', COUNT(*), MIN(created_at), MAX(created_at),
       COUNTIF(created_at > CURRENT_TIMESTAMP()), COUNTIF(DATE(created_at) > CURRENT_DATE())
FROM `bigquery-public-data.thelook_ecommerce.order_items`  -- same for orders, users
```

| Table | min `created_at` | max `created_at` | rows after now | rows dated after today |
|---|---|---|---:|---:|
| `order_items` | 2019-01-07 | **2026-10-29** | 1,412 | 1,216 |
| `orders` | 2019-01-07 | 2026-10-25 | 0 | 0 |
| `users` | 2019-01-02 | 2026-10-24 | 0 | 0 |

- **The dataset is regenerated daily**: `last_modified_time` of all tables is 2026-10-25 02:33–02:34 UTC. Row counts, brand rankings and every key number below shift from day to day. Reference SQL must run live (DD §16.1), and the seed must compute brands live (DD §7).
- **`order_items` has rows dated in the future** (up to 4 days ahead), and so do the item-level `shipped_at` (1,324 rows after now, max 2026-10-27), `delivered_at` (1,775, max 2026-11-01) and `returned_at` (652, max 2026-11-02). Item `created_at` differs from the order's `created_at` for 181,767 of 181,777 rows (−3 h to +96 h). → The scoped `order_items` CTE filters `created_at <= CURRENT_TIMESTAMP()` for every scope (DD §8.1). Duration metrics still filter the end timestamp (glossary §1.2).
- **Strong recency ramp** (synthetic generator): counted items per year grow every year (2019: 1,524 … 2025: 41,152; 2026 to date: 65,030); October 2026 alone has 13,497 items vs 7,950 in September; daily items rise from ~375 (2026-10-16) to ~1,950 (2026-10-24). Year-over-year growth is therefore ~100 % for most categories, and partial periods must never be compared with full ones.

### 1.3 Categorical columns

```sql
SELECT status, COUNT(*), SUM(sale_price), COUNTIF(returned_at IS NOT NULL), COUNTIF(shipped_at IS NOT NULL), COUNTIF(delivered_at IS NOT NULL)
FROM `bigquery-public-data.thelook_ecommerce.order_items` GROUP BY status;
```

| `order_items.status` | Items | `sale_price` sum | has `shipped_at` | has `delivered_at` | has `returned_at` |
|---|---:|---:|---:|---:|---:|
| Shipped | 53,895 | 3,217,315 | all | none | none |
| Complete | 45,730 | 2,703,273 | all | all | none |
| Processing | 36,642 | 2,185,640 | none | none | none |
| Cancelled | 27,524 | 1,647,880 | none | none | none |
| Returned | 17,986 | 1,053,476 | all | all | all |

- Exactly five values, capitalised as shown. Revenue excludes `Cancelled` and `Returned`. `Processing` and `Shipped` count as sold.
- `orders.status` equals the item status for every joined row (0 mismatches), but it is excluded from the `orders` CTE (DD §8.1, Q25).

| Column | Distinct values | Notes |
|---|---:|---|
| `products.brand` | 2,756 (+24 NULL) | 961 contain an apostrophe (`Levi's`, `Joe's Jeans`), 23 non-ASCII / mojibake (`NygÃ¥rd Collection`); many are personal names (Calvin Klein, Tommy Hilfiger, Ralph Lauren) or common words (`Champion`, `Columbia`) |
| `products.category` | 26 | 36 category×department pairs; full list in `content/catalog.yaml` |
| `products.department` | 2 | `Men`, `Women` |
| `products.name` | 27,309 (2 NULL) | free text, max 224 chars; contains digit runs such as `F526138377`, `[49002639827]`, `14-16-18-20-22` |
| `users.country` | 14 | China 34,050; United States 22,634; Brasil 14,540 (spelled `Brasil`); …; Austria 2 |
| `users.state` | 226 | |
| `users.city` | 7,832 | **967 rows hold the string `'null'`** (not SQL NULL) |
| `users.traffic_source` | 5 | `Search`, `Organic`, `Facebook`, `Email`, `Display` |
| `users.gender` | 2 | `M`, `F` |
| `users.age` | 12–70 | integer |

Query: `SELECT COUNT(DISTINCT brand), COUNTIF(brand IS NULL), COUNT(DISTINCT category), ... FROM products` and the per-column `GROUP BY` queries in the Stage 0 session.

### 1.4 Brands and orders

```sql
SELECT p.brand, ROUND(SUM(oi.sale_price),0) revenue, COUNT(*) items, COUNT(DISTINCT oi.user_id) customers
FROM `...order_items` oi JOIN `...products` p ON p.id = oi.product_id
WHERE oi.status NOT IN ('Cancelled','Returned') AND oi.created_at <= CURRENT_TIMESTAMP()
GROUP BY 1 ORDER BY revenue DESC LIMIT 25
```

| Rank | Brand | Revenue | Items | Customers |
|---:|---|---:|---:|---:|
| 1 | Calvin Klein | 147,315 | 2,307 | 2,269 |
| 2 | Diesel | 144,184 | 1,052 | 1,036 |
| 3 | 7 For All Mankind | 139,211 | 881 | 875 |
| 4 | Carhartt | 138,389 | 1,962 | 1,926 |
| 5 | True Religion | 127,566 | 645 | 643 |
| 6 | Tommy Hilfiger | 86,081 | 1,208 | 1,195 |
| 7 | Joe's Jeans | 78,314 | 510 | 509 |
| 8 | Volcom | 78,215 | 1,342 | 1,326 |
| 9 | Ray-Ban | 77,077 | 658 | 654 |
| 10 | Columbia | 74,569 | 1,099 | 1,089 |

- Total counted revenue (all brands, to date): ~8.0 M. Even the top brand is < 2 % of it, so a brand manager's numbers are an order of magnitude smaller than the CEO's.
- **37,403 of 125,396 orders (30 %) contain items of more than one brand.** This confirms why order-level fields leak other brands (DD §8.1).
- Order frequency per customer (counted items): CEO 1 order 45,833 / 2 orders 14,358 / 3: 4,235 / 4: 1,630. For one brand scope (brand_manager_1), 3,689 customers have 1 order and 49 have 2. → The RFM pattern uses fixed F buckets, not NTILE (glossary).
- Names to keep in mind for tests: `Nike` exists (81 products), so do `Champion`, `Columbia`, `Levi's`, `Joe's Jeans`; `Adidas`, `Puma` and `Guess` do not.

---

## 2. Bytes scanned and the bytes cap

Measured with dry-run and execution of the DD §8.1 CTEs (`brand IN UNNEST(@brands)` as a query parameter for the brand scope):

| Query (logical names) | CEO processed | Brand scope processed | Billed (both) |
|---|---:|---:|---:|
| revenue by country (order_items ⋈ users) | 9,707,642 | 10,057,421 | 41,943,040 |
| orders per month (orders) | 5,147,728 | 5,497,507 | 41,943,040 |
| `SELECT * FROM users LIMIT 3` | 9,905,276 | 10,255,055 | 41,943,040 |
| golden reference SQL (15 queries, §6) | 4.0–11.3 M | 4.4–11.7 M | 41,943,040 |

- Every scoped query references all four base tables through the CTE chain, so **billed bytes are a flat 40 MiB** (4 tables × the 10 MiB per-table minimum). A brand filter adds ~0.35 MB processed (the `brand` column of `products`).
- Metadata queries (`INFORMATION_SCHEMA`) bill 10 MiB each.
- **Proposed bytes cap: `maximum_bytes_billed = 1,000,000,000` (1 GB).** That is ~25× a typical scoped query, and ~22× a query that reads every column of all four tables. It still stops runaway queries such as cross joins (whose cost shows in processed bytes, and in slot time rather than bytes). Set in `config.yaml` (`bigquery.bytes_cap`).
- Free tier: BigQuery on-demand gives 1 TiB of queries per month free, so ~25,000 scoped queries per month at 40 MiB each.
- CTE name resolution: `FROM USERS`, ``FROM `users` `` and `FROM Order_Items` all resolve to the CTEs (verified live: same counts as `FROM users`). Table and CTE names are case-insensitive, so the validator and the CTE injector must compare names case-insensitively.

---

## 3. Gemini models and free tier

Source: Google AI for Developers, read 2026-10-25.

| Fact | Value | Source |
|---|---|---|
| Newest stable Flash | `gemini-3.8-flash` ("New Stable", Sep 2026); 1,048,576 input / 65,536 output tokens; function calling and structured outputs supported | [models](https://ai.google.dev/gemini-api/docs/models), [3.8 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash) |
| Previous stable Flash | `gemini-3.6-flash` | [models](https://ai.google.dev/gemini-api/docs/models) |
| Newest stable Flash-Lite | `gemini-3.5-flash-lite` (Jul 2026); same limits and features | [3.5 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite) |
| Previous stable Flash-Lite | `gemini-3.1-flash-lite` | [models](https://ai.google.dev/gemini-api/docs/models) |
| Pro | only `gemini-3.1-pro-preview` listed; `gemini-4-argon` is not on the free tier | [models](https://ai.google.dev/gemini-api/docs/models), [pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| Google's recommendation | "For any new projects, use our latest models: 3.5 Flash-Lite or 3.8 Flash." | [models](https://ai.google.dev/gemini-api/docs/models) |
| Free tier | both `gemini-3.8-flash` and `gemini-3.5-flash-lite`: input and output "Free of charge" | [pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| Paid price, `gemini-3.8-flash` | $0.75 in / $3.75 out per 1M tokens through 2026-12-31; $1.50 / $7.50 from 2027-01-01 | [pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| Paid price, `gemini-3.5-flash-lite` | $0.30 in / $2.50 out per 1M tokens | [pricing](https://ai.google.dev/gemini-api/docs/pricing) |
| Limit dimensions | RPM, TPM (input), RPD; some models also TPD | [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) |
| Scope of limits | "applied per project, not per API key"; "vary depending on the specific model" | [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) |
| RPD reset | "at midnight Pacific time" | [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) |
| Free-tier numbers | **not published** on the rate-limits page; it points to AI Studio (`aistudio.google.com/rate-limit`) per project | [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits) |

**Role assignment (config.yaml):**
- `cheap` = `gemini-3.5-flash-lite`, fallback `gemini-3.1-flash-lite`.
- `expensive` (standard profile) = `gemini-3.8-flash`, fallback `gemini-3.6-flash`, then `gemini-3.5-flash-lite`.
- `free` profile: both roles on `gemini-3.5-flash-lite` → `gemini-3.1-flash-lite`.
- Free-tier quotas are per model, so each fallback step has its own quota (DD §14.1).
- Free-tier numbers stay unverified: the official page does not publish them. DD §1 cites developer reports (~20 RPD for 3.x Flash, ~500 RPD for Flash-Lite); third-party guides disagree (500–1,000 RPD for Flash-Lite). The README must tell the reviewer to read their own limits in AI Studio. The 20-RPD figure for Flash is why the `free` profile exists.

---

## 4. Per-minute vs daily 429 (for Stage 3)

What the official pages say:
- The troubleshooting page names `429 RESOURCE_EXHAUSTED` as retryable with exponential backoff and jitter. It shows no error body. The Python SDK retries transient errors itself (up to 4 times, ~1 s initial, 60 s max). [troubleshooting](https://ai.google.dev/gemini-api/docs/troubleshooting)
- The API errors page (Interactions API) separates the two cases by code: `rate_limit_exceeded` (429, "per-minute or per-second request or token limit") vs `quota_exceeded` (429, "exceeded your daily quota"), with body `{"error": {"code": "<snake_case>", "message": "..."}}`. [api-errors](https://ai.google.dev/gemini-api/docs/api-errors)
- Spend-limit overruns (paid tiers) also return `429 RESOURCE_EXHAUSTED`. [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits)
- Google's error model (AIP-193): `{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": ..., "details": [...]}}` with standard detail payloads. [AIP-193](https://google.aip.dev/193)

**Not verified by an official page, from known `generateContent` behaviour; Stage 3 must capture a real body once.** The `generateContent` 429 body carries `details` with:
- `type.googleapis.com/google.rpc.QuotaFailure`, whose `violations[].quotaId` names the window, e.g. `GenerateRequestsPerMinutePerProjectPerModel-FreeTier` vs `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, and `quotaMetric` such as `generativelanguage.googleapis.com/generate_content_free_tier_requests`;
- `type.googleapis.com/google.rpc.RetryInfo` with `retryDelay` (e.g. `"37s"`).

**Classification rule (binding for Stage 3, in `contracts.md` §4.3):**
1. Daily if any `quotaId` contains `PerDay`, or the error code is `quota_exceeded`, or the message mentions "per day" / "daily".
2. Otherwise per-minute: `quotaId` contains `PerMinute`, the code is `rate_limit_exceeded` / `too_many_requests`, or the 429 has no detail at all.
3. Retry wait = `RetryInfo.retryDelay` or the `Retry-After` header if present, otherwise exponential backoff with jitter. If the wait exceeds the remaining turn budget, do not retry; fall back instead.

---

## 5. Demo executives: seed rule

Implemented by `scripts/seed_executives.py` (Stage 2). Brands are computed live on the reviewer's machine.

1. Rank brands by counted revenue over all data to date: `status NOT IN ('Cancelled','Returned') AND created_at <= CURRENT_TIMESTAMP()`, `brand IS NOT NULL`, ties broken by brand name ascending.
2. Take the top 6. `brand_manager_1` gets ranks 1, 3, 5 and `brand_manager_2` gets ranks 2, 4, 6. The managers don't overlap and have comparable volume: ~415k vs ~370k revenue and 3.7k vs 4.1k customers. Each of the six brands sells in both departments and in 6–22 categories.
3. Executives:

| exec_id | display_name | scope_all | brands on 2026-10-25 |
|---|---|---|---|
| `ceo` | Morgan Lee (CEO) | true | — (all) |
| `brand_manager_1` | Sam Rivera (Brand Manager) | false | Calvin Klein, 7 For All Mankind, True Religion |
| `brand_manager_2` | Alex Chen (Brand Manager) | false | Diesel, Carhartt, Tommy Hilfiger |

4. The seed is idempotent: it upserts executives and replaces each manager's brand rows in one transaction. It prints the chosen brands. Display names are synthetic employee names (DD §7).

Because the data is regenerated daily, ranks 1–6 may reorder on another day. Any non-overlapping split is acceptable, and the golden set resolves entities per persona (§6), so nothing downstream depends on these particular brands.

---

## 6. Golden set reference SQL: live run

All 14 reference queries of `eval/golden_set.yaml` and the three query resolvers were run on 2026-10-25, wrapped in the DD §8.1 CTEs, for the CEO and both brand scopes of §5. All succeeded. Processed bytes per query ranged from 4.0 to 11.7 MB; billed was 40 MiB each.

| Case | CEO key number | brand_manager_1 | brand_manager_2 |
|---|---|---|---|
| B01 top product revenue | 9,030 | 3,432 | 5,306 |
| B02 top country (China) revenue | 2,760,450 (34.3 %) | 142,913 (34.5 %) | 124,840 (33.9 %) |
| B03 fastest-growing category YTD | Jumpsuits & Rompers +123.9 % | Leggings +347.7 % (small base) | Plus +349.3 % (small base) |
| B04 resolver (state_x vs state_y), revenue/customer | Hebei 116.96 vs Guangdong 125.68 | Texas 97.16 vs Guangdong 117.04 | Texas 81.61 vs Guangdong 95.96 |
| B05 resolver product_a revenue | 9,030 (id 25348) | 3,432 (id 21118) | 5,306 (id 24083) |
| B06 churn rate, last month vs previous 5 | 27.89 % vs 28.77–30.52 % → **no spike** | 33.83 % vs 28.49–33.24 % → within range, **no clear spike** | 31.82 % vs 29.75–35.24 % → **no spike** |
| B07 Q1 revenue / orders / AOV | 617,440 / 7,154 / 86.31 | 30,614 / 275 / 111.32 | 27,351 / 318 / 86.01 |
| B09 customers segmented (RFM) | 66,057 | 3,738 | 4,067 |
| B10 top customer spend | 1,662.26 | 695.00 | 898.00 |
| B11 last full month revenue | 348,843 | 13,673 | 14,949 |
| B12 top product MTD revenue | 1,500 | 666 | 736 |
| B13 AOV last month / month before | 83.44 / 82.67 | 91.15 / 118.83 | 88.46 / 73.31 |
| B14 slowest delivery category (days) | 3.48 | 6.63 | 5.10 |
| B15 YTD revenue (all age/gender groups) | 2,834,571 | 145,259 | 124,011 |

- The same question gives visibly different answers per persona, as required by `clarifications.md`.
- B06 is the honesty case: on this day none of the three personas has a clear spike, so the agent must say so.
- `own_brand` resolves to Calvin Klein (brand_manager_1) and Diesel (brand_manager_2). `other_brand` comes from the identity store: Diesel for brand_manager_1, Calvin Klein for brand_manager_2.

---

## 7. PII patterns measured on real values

Used to calibrate the result scan (DD §8.3) and input masking (DD §8.5). The PII columns were only counted, never displayed.

- **False positives:** 0 hits for the email, phone and address patterns of `eval/security_cases.yaml` over 66,016 real strings: product names, brands, distinct cities and distinct states. A naive "9+ digits with separators" phone pattern matches 70 product names (`F526138377`, `SIZE 14-16-18-20-22`), so the result scan uses the stricter phone pattern.
- **Recall on the PII columns themselves:** the email pattern matches 100 % of `users.email`. The address pattern (house number + 1–3 words + street suffix, end or `Apt/Suite`) matches 28.8 % of `users.street_address`. Faker-style addresses use many uncommon suffixes, so the address scan is a weak, defense-in-depth layer, and the real protection is that the column is absent from the CTEs.
- **Luhn trap:** `2023 2024 2025 2026` passes the Luhn check. The card rule therefore also requires a card-network prefix (4; 51–55; 2221–2720; 34/37; 6011/65/644–649) and a card-shaped grouping.

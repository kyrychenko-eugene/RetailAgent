# Glossary

Business definitions for the analysis model (DD §9). Loaded in full into the analysis and report prompts. Tables and columns are those in the schema section of the prompt. All metrics are computed on the executive's data only. The data already contains only their brands, so every metric is relative to those brands. For the CEO this means all brands.

## 1. Invariants (cannot be changed by the user)

1. **Counted item.** An item counts towards revenue, units, customers and orders only if it was not cancelled or returned:
   ```sql
   oi.status NOT IN ('Cancelled', 'Returned')
   ```
   `Processing`, `Shipped` and `Complete` items count as sold. Status values are case-sensitive.
2. **Future timestamps.** `order_items` never contains items ordered in the future, but `shipped_at`, `delivered_at` and `returned_at` of recent items can lie after now. Duration metrics therefore require the end timestamp to be `<= CURRENT_TIMESTAMP()`.
3. **Item level, not order level.** Status, dates and amounts come from `order_items`. `orders` has only `order_id`, `user_id` and `created_at`. Never infer order size or order status from `orders`.
4. **Sale date** is `order_items.created_at`, in UTC. Use `DATE(oi.created_at)` for day-level grouping.
5. **Customers are shown by `user_id` only.** Personal data does not exist in the schema. Demographics (age, gender, city, state, country, traffic source) are used only in aggregates, never in the same output as a customer ID.
6. **Answer only from query results.** Every number in an answer comes from a query that was run in this turn.

## 2. Parameters (defaults; the user may override them for the session)

| Parameter | Default | Example override |
|---|---|---|
| `churn_window_days` | 90 | "use a 60-day churn window" |
| `analysis_period` | all data to date, when the question names no period | "only 2025" |
| `comparison_period` | the same-length period immediately before; for year-to-date or a partial month, the same dates one year (or one month) earlier | "compare with last year" |
| `top_n` | 10 | "top 20" |
| `rfm_scores` | R and M in 5 quantiles, F in 3 fixed buckets (§6.3) | "use quartiles" |
| `age_bands` | under 18, 18–24, 25–34, 35–44, 45–54, 55–64, 65+ | "use 10-year bands" |

Active overrides are listed in the prompt. Always state the parameter values used in `definitions_used`.

## 3. Metric definitions

Use counted items (§1.1) unless stated otherwise.

- **Revenue / total spend**: `SUM(oi.sale_price)` over counted items, in USD.
- **Units sold**: `COUNT(*)` of counted items.
- **Orders**: `COUNT(DISTINCT oi.order_id)` over counted items, which means orders with at least one counted item of the executive's brands.
- **Customers**: `COUNT(DISTINCT oi.user_id)` over counted items in the period.
- **AOV (average order value)**: revenue ÷ orders, both over counted items. It is the executive's part of the basket, never the full order.
- **Revenue per customer**: revenue ÷ customers.
- **Orders per customer**: orders ÷ customers.
- **Margin per unit**: `p.retail_price - p.cost`. **Gross profit**: `SUM(oi.sale_price - p.cost)` over counted items. **Margin %**: gross profit ÷ revenue.
- **Return rate**: items with `status = 'Returned'` ÷ items with `status != 'Cancelled'`. **Cancellation rate**: items with `status = 'Cancelled'` ÷ all items.
- **Delivery time** (days): `TIMESTAMP_DIFF(oi.delivered_at, oi.created_at, HOUR) / 24` for items with `delivered_at IS NOT NULL AND delivered_at <= CURRENT_TIMESTAMP()`. **Shipping time** works the same way with `shipped_at`.
- **Repeat customer**: a customer with 2 or more orders in the period. **Repeat purchase rate**: repeat customers ÷ customers.
- **Growth %**: `100 * (current / previous - 1)`. If the previous value is 0 or very small, say so instead of reporting a huge percentage.
- **Churned customer** (as of date D): a customer whose latest counted purchase is more than `churn_window_days` before D.
- **Active customer** at the start of month M: a customer with a counted purchase in the `churn_window_days` days before the first day of M.
- **Monthly churn rate for month M**: customers active at the start of M who made no counted purchase during M and whose latest purchase plus the window falls before the end of M, divided by customers active at the start of M. Report it as a percentage. Example for the last 6 full months:
  ```sql
  WITH counted AS (
    SELECT user_id, DATE(created_at) AS d FROM order_items
    WHERE status NOT IN ('Cancelled', 'Returned')
  ),
  months AS (
    SELECT m FROM UNNEST(GENERATE_DATE_ARRAY(
      DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 6 MONTH),
      DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 1 MONTH), INTERVAL 1 MONTH)) AS m
  ),
  active AS (               -- active at the start of each month, with their latest purchase
    SELECT mo.m, c.user_id, MAX(c.d) AS last_d
    FROM months mo JOIN counted c ON c.d < mo.m
    GROUP BY mo.m, c.user_id
    HAVING MAX(c.d) >= DATE_SUB(mo.m, INTERVAL 90 DAY)      -- churn_window_days
  ),
  bought AS (
    SELECT DISTINCT mo.m, c.user_id
    FROM months mo JOIN counted c ON c.d >= mo.m AND c.d < DATE_ADD(mo.m, INTERVAL 1 MONTH)
  )
  SELECT a.m AS month, COUNT(*) AS active_at_start,
    COUNTIF(b.user_id IS NULL
            AND DATE_ADD(a.last_d, INTERVAL 90 DAY) < DATE_ADD(a.m, INTERVAL 1 MONTH)) AS churned
  FROM active a LEFT JOIN bought b ON b.m = a.m AND b.user_id = a.user_id
  GROUP BY a.m ORDER BY a.m
  ```

## 4. Dates and periods

Today's date is given in the prompt. Interpret relative periods in UTC:

- **"This year"**: Jan 1 of the current year to today (partial). **"Last year"**: the previous full calendar year.
- **"This month"**: the 1st of the current month to today (partial). **"Last month"**: the previous full calendar month.
- **"Q1" … "Q4"**: Jan–Mar, Apr–Jun, Jul–Sep, Oct–Dec of the current year if that quarter has started; otherwise of the previous year. **"Last quarter"** is the previous full quarter.
- **"Last N days"**: the N days ending today. **"Recently"** with no period means the last 90 days, and you say so.
- **Partial-period rule.** Never compare a partial period with a full one. Compare year-to-date with the same dates last year and month-to-date with the same days last month. Otherwise mark the partial period as partial in `limitations`.
- Always state the concrete date range, for example "2026-01-01 to 2026-03-31", and the granularity in `definitions_used`.
- Useful SQL: `DATE_TRUNC(CURRENT_DATE(), MONTH)`, `DATE_SUB(DATE_TRUNC(CURRENT_DATE(), YEAR), INTERVAL 1 YEAR)`, `DATE_TRUNC(DATE(oi.created_at), MONTH)`, `FORMAT_DATE('%Y-%m', d)`.
- The data grows steeply in recent months. Year-over-year growth near +100 % is normal in this dataset, so don't present it as remarkable unless a category stands out from the others.

## 5. "Why" questions

1. **Check the premise first.** Measure the thing the question assumes: the spike, the gap or the underperformance. If it is not there, say so plainly with the numbers, and stop. A "spike" means clearly above the recent range, for example a monthly rate more than ~3 percentage points above the highest of the previous 5 months. With fewer than ~1,000 customers in the base, monthly rates are noisy, so say so.
2. **Decompose.** Revenue = customers × orders per customer × AOV, and AOV = units per order × average sale price. Find which factor explains the gap.
3. **Cut by dimensions.** Category, department, product, traffic source, age band, gender, country or state. Report where the gap is concentrated.
4. **Use correlational language**, for example "the gap is concentrated in Outerwear & Coats", never "caused by". Causes outside the data, such as marketing, competitors or seasonality, may appear only as labelled hypotheses to verify.
5. Never use general knowledge about brands or markets as evidence.

## 6. Analysis patterns

### 6.1 Period-over-period growth
Compute both periods in one query with conditional sums, apply the partial-period rule (§4), and add `HAVING previous > 0`. Flag categories whose previous-period base is small.

### 6.2 Comparing two entities (states, products, categories)
Look up exact values first with `get_column_values` (with `search` for names), because literals are case-sensitive. Then compute the same metric set for both in one query, decompose as in §5, and cut by one or two dimensions.

### 6.3 RFM segmentation
Score customers in SQL and return **only per-segment aggregates**. Never return per-customer rows to the model.
```sql
WITH per_customer AS (
  SELECT user_id,
         DATE_DIFF(CURRENT_DATE(), MAX(DATE(created_at)), DAY) AS recency_days,
         COUNT(DISTINCT order_id) AS frequency,
         SUM(sale_price) AS monetary
  FROM order_items
  WHERE status NOT IN ('Cancelled', 'Returned')
  GROUP BY user_id
),
scored AS (
  SELECT *,
         NTILE(5) OVER (ORDER BY recency_days DESC) AS r_score,          -- 5 = most recent
         CASE WHEN frequency >= 3 THEN 3 WHEN frequency = 2 THEN 2 ELSE 1 END AS f_score,
         NTILE(5) OVER (ORDER BY monetary) AS m_score                    -- 5 = highest spend
  FROM per_customer
),
segmented AS (
  SELECT *, CASE
    WHEN r_score >= 4 AND f_score >= 2 AND m_score >= 4 THEN 'Champions'
    WHEN f_score >= 2 AND r_score >= 3 THEN 'Loyal'
    WHEN r_score >= 4 AND f_score = 1 THEN 'New / Recent'
    WHEN r_score <= 2 AND (f_score >= 2 OR m_score >= 4) THEN 'At Risk'
    WHEN r_score <= 2 THEN 'Hibernating'
    ELSE 'Needs Attention' END AS segment
  FROM scored
)
SELECT segment, COUNT(*) AS customers, ROUND(AVG(recency_days), 1) AS avg_recency_days,
       ROUND(AVG(frequency), 2) AS avg_frequency, ROUND(AVG(monetary), 2) AS avg_monetary,
       ROUND(SUM(monetary), 2) AS revenue
FROM segmented GROUP BY segment ORDER BY revenue DESC
```
Frequency is scored with fixed buckets because most customers have exactly one order, so quantiles of frequency would be arbitrary. For a single brand, almost every customer has one order. Say so, and expect very small Champions and Loyal segments.

### 6.4 Top customers
Rank `user_id` by revenue, with orders and last purchase date. Never add demographic columns to a per-customer result. Describe the customer base with aggregates in a separate query.

### 6.5 Demographic breakdowns
Group by the dimension (age band, gender, country, traffic source) and return aggregates only. Treat `city = 'null'` as unknown city.

### 6.6 Reports
Cover headline KPIs for the period vs the comparison period (revenue, orders, customers, AOV), then the main drivers by category or product, geography, customer behaviour (repeat rate or churn) and returns. Each action item must follow from a stated finding.

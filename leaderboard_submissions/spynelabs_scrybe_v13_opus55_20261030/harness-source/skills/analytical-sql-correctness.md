---
name: analytical-sql-correctness
description: Getting analytical SQL numbers right - grain, fan-out, NULLs, windows, ratios, percentiles, date ranges, verification
platforms: [any]
---
**Grain and fan-out**
- State the grain of every table and of the result ("one row per order per day"). Check it: `select k, count(*) from t group by k having count(*) > 1 limit 5;`
- Before and after each join compare `count(*)` and `count(distinct key)`. If `count(*)` grows, the join fanned out and `sum()` double-counts.
- Aggregate the many-side to the join key FIRST, then join: `join (select order_id, sum(qty) q from items group by 1) i using (order_id)`.
- `count(distinct x)` hides fan-out for counts but not for sums/averages.

**Join type**
- `inner` drops unmatched rows silently; use `left` when the question is about all rows of the base entity (e.g. "including those with zero").
- A `where` filter on the right table of a LEFT JOIN turns it into an inner join; put it in `on` instead.
- Anti-join: `left join ... where r.k is null` or `not exists (...)`.
- Check unmatched keys: `select count(*) from a left join b on a.k = b.k where b.k is null;`

**NULL semantics**
- `count(*)` counts rows, `count(col)` skips NULLs, `avg(col)` divides by non-NULL count only. Decide if NULL means 0 (`coalesce(x, 0)`) or unknown.
- `x = NULL` / `x <> 'a'` are never true for NULL rows; `where status <> 'X'` drops NULL statuses.
- `not in (subquery)` returns no rows if the subquery yields any NULL; use `not exists`.
- `NULL` keys never match in joins; `group by` puts all NULLs in one group.
- `sum` over zero rows is NULL, not 0.

**Distinct counting**
- `count(distinct a, b)` is not portable; use `count(distinct a || '|' || b)` or a subquery `select count(*) from (select distinct a, b ...)`.
- Normalise before counting (`lower(trim(email))`); mixed case/whitespace inflates distinct counts.

**Windows**
- With `order by` and no frame, the default frame is `range between unbounded preceding and current row`: ties (peers) are included together. Use `rows between ...` for strict running totals.
- `last_value(x)` needs `rows between unbounded preceding and unbounded following`.
- Ties in `row_number()` / `first_value` ordering make results nondeterministic: add the unique key as tiebreaker (`order by ts desc, id desc`); when the task names the column a window orders by (or one it must not use), order by that column and then the unique key only, never by another column. `rank`, `dense_rank`, `percent_rank`, `cume_dist` and `ntile` with an explicit task ordering are different: a tiebreaker changes their values, so add one only where the task asks for it, and only to the column it names.
- NULL in checks and CASE: `where not (a = b)` and `abs(x - 1) > 0.01` silently skip rows where the expression is NULL; count those rows explicitly. A NULL input falls through `case when x >= t ...` into the `else` branch; when a score or tier must cover every row, decide NULL handling explicitly (the task's rule, else `coalesce` with the stated neutral value) instead of letting NULL land in a real category.
- Nullable keys: filter or keep rows with a NULL key per row before grouping or deduplicating on that key; `group by key` puts all NULL keys into one bogus group.
- Windows over dates: an "N-day" window is a calendar window (`range between interval (N-1) day preceding and current row`, or a date spine), not N rows; "previous month/week" is the previous calendar period, not the previous row when periods are missing. Daily averages over a period divide by the days in the period (zero-filled), not by the days with data.
- `rank` leaves gaps, `dense_rank` does not; pick per the question ("top 3 values" vs "top 3 rows").
- Dedup: `row_number() over (partition by key order by updated_at desc, id desc) = 1`.

**Ratios and averages**
- Average of ratios (`avg(a/b)` per entity) != ratio of sums (`sum(a)/sum(b)`). Read the question: "average per customer" vs "overall rate". Write which one you used.
- Integer division truncates in Postgres/SQLite (`1/2 = 0`); multiply by `1.0` first. Guard with `nullif(den, 0)`.
- Percentages: decide 0-1 vs 0-100 and keep it consistent.

**Percentiles / medians**
- Postgres: `percentile_cont(0.5) within group (order by x)` (aggregate only).
- Snowflake: `median(x)`, `percentile_cont(0.5) within group (order by x)`.
- DuckDB: `median(x)`, `quantile_cont(x, 0.5)`, `quantile_disc(x, 0.5)`.
- SQLite: none built in; compute in Python (`Series.median()`, `quantile(0.5)` = continuous).
- `_cont` interpolates (even n: mean of the two middle values); `_disc` returns an actual value.

**Rounding**
- Round only the final output, never intermediate values.
- SQL `round` is half away from zero (numeric); Python `round()` and pandas/numpy `round` are half-to-even; floats may show `x.xx4999`.

**Date ranges and buckets**
- Use half-open ranges: `ts >= '2024-01-01' and ts < '2024-02-01'`. `between '2024-01-01' and '2024-01-31'` misses times after midnight on the 31st.
- Confirm whether the question's range is inclusive of the end date.
- Bucket with `date_trunc('month', ts)` (or `strftime('%Y-%m', ts)` in SQLite); week start differs by engine/setting.
- Missing periods produce no rows; join to a date spine if zero-filled series are needed.
- Convert timestamps to the intended time zone BEFORE truncating to day/month.
- "Last N days" depends on the anchor: today vs the max date in the data. Use the data's max date when the dataset is historical, and say so.

**Verify**
- Recompute 2-3 result rows independently (a simpler query on raw tables, or by hand) and compare.
- Cross-check totals: sum of groups = overall total; counts by category add up to `count(*)`.
- Look at min/max/null counts of every column used in a filter or metric.
- Print the query result with enough precision before rounding to spot float artefacts.

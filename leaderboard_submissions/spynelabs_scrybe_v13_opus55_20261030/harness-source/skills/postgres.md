---
name: postgres
description: PostgreSQL catalog lookup, casts, date/interval math, DISTINCT ON, JSONB, regex and NULL ordering
platforms: [postgres, dbt]
---
**Schemas and identifiers**
- Unquoted names fold to lower-case; `"OrderID"` must be quoted exactly as created.
- `show search_path;` `set search_path to analytics, public;` Prefer schema-qualified names `sch.t`.
- psql: `\dn` schemas, `\dt sch.*` tables, `\d+ sch.t` columns, `\x` expanded rows, `-At -c "..."` for script output.

**Catalog**
- `select table_schema, table_name, column_name, data_type from information_schema.columns where table_schema not in ('pg_catalog', 'information_schema') order by 1, 2, ordinal_position;`
- Fast row estimates: `select relname, reltuples::bigint from pg_class where relkind = 'r';`
- Views: `information_schema.views`; definitions: `select pg_get_viewdef('sch.v'::regclass, true);`

**Casts and parsing**
- `x::numeric`, `cast(x as int)`, `'2024-01-31'::date`, `to_date(s, 'DD/MM/YYYY')`, `to_timestamp(s, 'YYYY-MM-DD HH24:MI:SS')`, `to_timestamp(epoch_secs)`.
- No `try_cast`. Guard with a regex: `case when s ~ '^-?\d+(\.\d+)?$' then s::numeric end`. PG16+: `pg_input_is_valid(s, 'numeric')`.
- Strip formatting first: `replace(replace(s, ',', ''), '$', '')::numeric`.

**Numbers**
- Integer division truncates: `7/2 = 3`. Use `7::numeric/2` or `7 * 1.0 / 2`.
- `round(double precision, int)` does not exist: `round(x::numeric, 2)`. `round` on numeric rounds half away from zero.
- `avg(int)` returns numeric; `sum(int)` returns bigint.

**Date and time**
- `date - date` = integer days. `timestamp - timestamp` = interval.
- `age(b, a)` gives a symbolic interval (`1 year 2 mons 3 days`); `date_part('day', interval)` is only the day COMPONENT, not total days.
- Total elapsed (timestamps): `extract(epoch from (b - a)) / 86400` days; months: `(extract(year from age(b,a))*12 + extract(month from age(b,a)))`.
- `date_trunc('month', ts)`, `date_trunc('week', ts)` (Monday). `extract(dow from d)` 0 = Sunday; `extract(isodow from d)` 1 = Monday.
- Add: `d + interval '1 month'` (`2024-01-31` -> `2024-02-29`, returns timestamp; cast `::date`), `d + 7` (days, stays DATE).
- `timestamptz` is stored as UTC and shown in session `TimeZone`: `set time zone 'UTC';`
- `ts_tz at time zone 'America/New_York'` -> local timestamp; `ts at time zone 'UTC'` -> timestamptz.
- Calendars: `select g::date as d from generate_series(date '2024-01-01', date '2024-12-31', interval '1 day') g;`

**Query patterns**
- One row per key: `select distinct on (cust_id) * from orders order by cust_id, created_at desc;` (ORDER BY must start with the DISTINCT ON keys).
- No QUALIFY: wrap `row_number() over (...)` in a subquery/CTE and filter `rn = 1`.
- Conditional aggregates: `count(*) filter (where status = 'paid')`, `sum(amt) filter (where ...)`.
- Median/percentile: `percentile_cont(0.5) within group (order by x)` (aggregate only, not a window function; no `median()`).
- `string_agg(x, ', ' order by x)`, `array_agg(distinct x)`, `bool_or(cond)`.

**JSONB**
- `j->'a'` (jsonb), `j->>'a'` (text), `j#>>'{a,b}'` (nested text), `(j->>'amt')::numeric`.
- `j @> '{"status": "paid"}'`, key exists: `j ? 'a'`, `jsonb_typeof(j->'a')`.
- Arrays to rows: `select t.id, e->>'sku' from t cross join lateral jsonb_array_elements(t.j->'items') e;`
- `jsonb_each_text(j)` for key/value pairs; `json` (not jsonb) supports `->`/`->>` but not `@>`/`?`.

**Text matching**
- `ilike '%abc%'` case-insensitive; `~` regex, `~*` case-insensitive regex, `!~` negation (unanchored).
- `substring(s from 'id=(\d+)')` returns the first group; `(regexp_match(s, 'id=(\d+)'))[1]`.
- `regexp_replace(s, p, r)` replaces first match only; add `'g'` flag for all.
- Full text: `to_tsvector('english', body) @@ websearch_to_tsquery('english', 'late delivery')`.
- `=` is case- and trailing-space-sensitive for text; normalise with `lower(trim(x))`.

**NULLs**
- ASC sorts NULLs LAST, DESC sorts NULLs FIRST: `order by x desc limit 1` can return NULL. Add `nulls last`.
- `is distinct from` for NULL-safe comparison.

**Verify**
- `explain select ...` before heavy queries; `set statement_timeout = '120s';` in exploratory sessions.

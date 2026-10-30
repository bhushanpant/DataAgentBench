---
name: snowflake
description: Snowflake SQL semantics, metadata lookup, semi-structured data, time zones and dbt-snowflake config
platforms: [snowflake, dbt]
---
**Identifiers**
- Unquoted names are stored UPPER-case and match case-insensitively: `select id from orders` = `ID`/`ORDERS`.
- Quoted names are exact: `"id"` is a different column from `ID`. Tables loaded by tools that quote (e.g. pandas `write_pandas`) often have lower/mixed-case names that MUST be quoted.
- Metadata stores unquoted names upper-case: `where table_name = 'ORDERS'`, not `'orders'`.

**Metadata**
- `show databases; show schemas in database db; show tables in schema db.sch;`
- `show columns in table db.sch.t;` / `describe table db.sch.t;`
- `select table_name, column_name, data_type from db.information_schema.columns where table_schema = 'SCH' order by 1, ordinal_position;`
- Query SHOW output: `select "name", "rows" from table(result_scan(last_query_id()));` (columns are lower-case, quoted).
- Row counts without a scan: `rows` column of `show tables`, or `information_schema.tables.row_count`.

**Semi-structured (VARIANT / OBJECT / ARRAY)**
- Path: `v:customer.name`, `v:items[0]`, `v['Key With Space']`. Keys match exact case (`Name` and `name` differ) even though unquoted column names do not.
- Path results are VARIANT: cast with `::string`, `::number(38,2)`, `::date`. Without `::string`, strings keep JSON quotes in comparisons/output.
- Arrays to rows: `select t.id, f.index, f.value:sku::string from t, lateral flatten(input => t.v:items) f;`
- `flatten(..., outer => true)` keeps rows whose array is empty/NULL. Objects: `f.key`, `f.value`.
- `parse_json(s)`, `try_parse_json(s)`, `typeof(v)`, `array_size(v)`, `object_keys(v)`; `is_null_value(v:x)` detects JSON null (distinct from missing/SQL NULL).
- Build: `object_construct('k', col)`, `array_agg(x) within group (order by y)`.

**Query features**
- `qualify row_number() over (partition by k order by ts desc) = 1` - dedup without subquery.
- `group by all`, `select * exclude (c1, c2)`, `iff(c, a, b)`, `nvl`, `zeroifnull`, `div0(a, b)`.
- String `=` compares exact case; use `ilike`, `upper(a) = upper(b)`, or `collate`.
- `a || NULL` is NULL; wrap parts in `coalesce(x, '')`.

**Time**
- `TIMESTAMP` defaults to `TIMESTAMP_NTZ` (wall-clock, no zone). `_LTZ` stores UTC, displays in session `TIMEZONE`. `_TZ` stores an offset.
- `current_timestamp()` returns LTZ; results depend on `alter session set timezone = 'UTC';` - check with `show parameters like 'TIMEZONE' in session;`.
- Convert: `convert_timezone('UTC', 'America/New_York', ntz_ts)` (3-arg, returns NTZ); `convert_timezone('America/New_York', ts)` (2-arg, returns TZ).
- `datediff(unit, a, b)` counts unit BOUNDARIES crossed: `datediff('year', '2023-12-31', '2024-01-01') = 1`; `datediff('day', ...)` ignores time of day. For elapsed time use a smaller unit and divide.
- `dateadd('month', 1, '2024-01-31'::date) = 2024-02-29` (clamps to month end). `last_day(d)`, `date_trunc('month', d)`.
- `dayofweek`, `week`, `date_trunc('week')` depend on `WEEK_START` / `WEEK_OF_YEAR_POLICY`; use `dayofweekiso`, `weekiso`, `yearofweekiso` for fixed ISO semantics.
- Parse: `to_date(s, 'YYYY-MM-DD')`, `to_timestamp(s, 'YYYY-MM-DD HH24:MI:SS')`, `to_timestamp(epoch_seconds)`.

**Numbers**
- No integer division operator: `7/2 = 3.500000`. Use `floor(a/b)` / `trunc(a/b)`; guard zero with `div0` or `nullif(b, 0)`.
- `NUMBER(p,s)` arithmetic changes scale; `avg`/division of integers yields a fixed scale. Cast to `float` or `number(38,10)` if more digits matter; round only at the end.
- `round(x, n)` rounds half away from zero; `round(x, n, 'HALF_TO_EVEN')` for banker's rounding (NUMBER input).
- Safe parsing: `try_to_number(s)`, `try_to_decimal(s, 18, 2)`, `try_to_date(s)`, `try_to_timestamp(s)`, `try_cast(s as int)` (string input only).

**Regex**
- `regexp_like` / `rlike` are implicitly anchored (whole string): use `'.*abc.*'` or `regexp_instr(s, 'abc') > 0`.
- Extract group: `regexp_substr(s, 'id=(\\d+)', 1, 1, 'e', 1)`. In single-quoted literals double each backslash, or use `$$id=(\d+)$$`.
- `regexp_replace(s, pat, rep)` replaces all; `regexp_count`; `split_part(s, ',', 1)`.

**MERGE**
```sql
merge into tgt t using src s on t.id = s.id
when matched then update set t.v = s.v
when not matched then insert (id, v) values (s.id, s.v);
```
- Duplicate source keys raise a nondeterministic-merge error: dedup `src` first.

**Cost and time**
- `select count(*)` / `min`/`max` on columns are metadata-fast; `select *` on big tables is not. Project only needed columns.
- Explore with `sample (1)` (percent), `sample (1000 rows)`, or `limit`; `limit` without `order by` is nondeterministic.
- `approx_count_distinct(x)` for sizing; exact `count(distinct x)` for answers.
- Set `alter session set statement_timeout_in_seconds = 600;` before heavy exploration.

**dbt-snowflake**
- Tables are `transient` by default (no fail-safe); `+transient: false` for permanent.
- `cluster_by: ['col']`, `snowflake_warehouse: 'WH'`, `copy_grants: true`, `query_tag`.
- Incremental strategies: `merge` (default, needs `unique_key`), `delete+insert`, `append`.
- Quoting: project `quoting:` defaults are false for database/schema/identifier. For source columns whose names need exact case, set `quote: true` on the column, or `quoting: {identifier: true}` on the source.
- Verify: `dbt show -s model --limit 5`; compare row counts with `select count(*) from {{ ref('model') }}`.

---
name: dialects
description: DuckDB vs Snowflake SQL differences and cross-database Jinja patterns
triggers: [snowflake, duckdb, datediff, date_diff, dateadd, interval, percentile, median, target.type]
---
Check `$DB_TYPE` / profile type first; write SQL for that engine. When the task asks for both backends, branch with `{% if target.type == 'duckdb' %} ... {% else %} ... {% endif %}`.

| Need | DuckDB | Snowflake |
|---|---|---|
| day difference | `date_diff('day', a, b)` or `datediff('day', a, b)` | `datediff('day', a, b)` |
| add days | `d + interval 30 day` / `d + interval '30' day` | `dateadd('day', 30, d)` |
| truncate | `date_trunc('month', d)` | `date_trunc('month', d)` |
| median | `median(x)` or `quantile_cont(x, 0.5)` | `median(x)` |
| percentile over window | `percentile_cont(0.5) within group (order by x) over ()` works | NOT as window; use `median(x) over ()` or a subquery |
| safe divide | `a / nullif(b, 0)` | `a / nullif(b, 0)` or `div0(a, b)` |
| integer division | `a // b` (DuckDB `/` returns double) | `floor(a / b)` / `trunc` |
| string agg | `string_agg(x, ',' order by x)` | `listagg(x, ',') within group (order by x)` |
| regex | `regexp_matches`, `regexp_extract(s, pat, grp)` | `regexp_like`, `regexp_substr(s, pat, 1, 1, 'e', grp)` |
| try cast | `try_cast(x as int)` | `try_cast(x as int)` (string input only) / `try_to_number` |
| boolean agg | `bool_or`, `bool_and` | `boolor_agg`, `booland_agg` |
| qualify | supported | supported |
| epoch | `epoch(ts)` | `date_part(epoch_second, ts)` |

- DuckDB `/` on integers returns a DOUBLE; `round(x, 2)` keeps DOUBLE. Snowflake integer `/` returns NUMBER with scale.
- Identifiers: Snowflake upper-cases unquoted names; never quote column names unless the project already does.
- `percent_rank()`, `cume_dist()`, `ntile(n)`, `dense_rank()`, `row_number()` work in both.
- inf / NaN only appear in DuckDB (double division); guard with `nullif(den, 0)` and, if needed, `case when isnan(x) or isinf(x) then null end`.

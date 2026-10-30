---
name: duckdb
description: DuckDB connections and locking, file readers, metadata, nested types, date and numeric semantics, dbt-duckdb
platforms: [duckdb, dbt, python]
---
**Connections and locks**
- One process may open a database file read-write; others then cannot open it at all. Several processes may share it only if ALL are read-only.
- Inspect safely: `duckdb -readonly file.duckdb` or `duckdb.connect('file.duckdb', read_only=True)`.
- Close your session (`con.close()`, exit CLI) before `dbt run`/`dbt build`, or dbt fails with a lock error.
- Other files: `attach 'other.duckdb' as o (read_only); select * from o.main.t;` `show databases;` `detach o;`

**Reading files directly**
- `select * from 'data/x.parquet';` `from read_parquet('data/*.parquet', union_by_name = true, filename = true);`
- CSV: `read_csv('f.csv', header = true)`; force types: `types = {'id': 'VARCHAR'}`, or `all_varchar = true` to keep leading zeros/raw text; `dateformat = '%d/%m/%Y'`.
- JSON: `read_json_auto('f.json')`, newline-delimited: `read_json('f.jsonl', format = 'newline_delimited')`.
- Check what auto-detection chose: `describe select * from read_csv('f.csv');`

**Metadata and profiling**
- `show tables;` `show all tables;` `describe t;` `summarize t;` (min/max/nulls/approx distinct per column).
- `select table_schema, table_name, column_name, data_type from information_schema.columns;`
- `duckdb_tables()`, `duckdb_columns()`, `duckdb_views()` for more detail.
- Identifiers are case-insensitive even when quoted; case is preserved for display.

**Nested types**
- LIST: `[1, 2, 3]`, 1-based `l[1]`, `len(l)`, `list_contains(l, x)`, `list_distinct(l)`, `l[2:3]`.
- Rows from a list: `select id, unnest(tags) as tag from t;` Empty/NULL lists produce no rows; to keep them: `unnest(case when len(tags) > 0 then tags else [NULL] end)`.
- STRUCT: `{'a': 1, 'b': 'x'}`, access `s.a` or `s['a']`; `select unnest(s) from t` expands fields to columns.
- JSON column: `j->'$.a.b'` (JSON), `j->>'$.a'` (VARCHAR), `json_extract_string(j, '$.a')`; cast after extracting.

**Dates and times**
- `date_diff('month', a, b)` counts boundaries crossed (`'2024-01-31'` to `'2024-02-01'` = 1).
- `date_sub('month', a, b)` counts COMPLETE units elapsed (same pair = 0). Pick deliberately.
- `date - date` gives integer days; `timestamp - timestamp` gives an INTERVAL; `epoch(ts)` gives seconds.
- Add: `d + interval 1 day`, `d + interval '3 months'`, `d + 7` (days, DATE only).
- `date_trunc('week', d)` starts Monday; `dayofweek(d)` 0 = Sunday, `isodow(d)` 1 = Monday.
- `date_trunc` returns TIMESTAMP even for DATE input (version-dependent); add `::date` when a date is expected.
- Parse: `strptime(s, '%Y-%m-%d %H:%M:%S')`, `strptime(s, ['%d/%m/%Y', '%Y-%m-%d'])` (list = try in order, error if none fit), `try_strptime(s, '%d/%m/%Y')` (NULL on failure). Format: `strftime(d, '%Y-%m')`.
- `timestamptz` displays in the `TimeZone` setting: `set timezone = 'UTC';`

**Numbers and strings**
- `/` always returns DOUBLE (`7/2 = 3.5`); `//` is integer division (`7//2 = 3`).
- `round(x, 2)` on DOUBLE stays DOUBLE; cast to `decimal(18,2)` for exact output.
- Regex is unanchored: `regexp_matches(s, 'abc')` = contains; `regexp_full_match` for whole string.
- `regexp_extract(s, 'id=(\d+)', 1)`, `regexp_extract_all`. `regexp_replace(s, p, r)` replaces only the FIRST match; add `'g'` for all.
- `ilike`, `starts_with`, `contains(s, 'x')`, `split_part`, `string_agg(x, ',' order by x)`.

**Useful SQL**
- `qualify row_number() over (partition by k order by ts desc) = 1`
- `group by all`, `select * exclude (c)`, `select * replace (round(x, 2) as x)`, `columns('^amt_')`.
- `arg_max(val, ts)`, `count_if(cond)`, `median(x)`, `quantile_cont(x, 0.9)`, `mode(x)`.
- NULLs sort last by default for both ASC and DESC; state `nulls first/last` when it matters.

**Python**
- `con.sql("select ...").df()`; `con.execute("select ?", [v]).fetchall()`.
- A pandas DataFrame in scope is queryable by name: `duckdb.sql("select count(*) from df")`.

**dbt-duckdb**
- `profiles.yml`: `type: duckdb`, `path: file.duckdb`, optional `extensions: [httpfs, json]`, `settings:`, `attach:`.
- Sources over files: `meta: {external_location: "data/{name}.parquet"}` on the source.
- `materialized: external` writes parquet/csv (`location`, `format`).
- Verify: `dbt show -s model --limit 5`, then (after dbt exits) query read-only.

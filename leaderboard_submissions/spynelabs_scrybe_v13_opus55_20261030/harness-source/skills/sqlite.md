---
name: sqlite
description: SQLite type affinity traps, TEXT/INTEGER dates, metadata pragmas, join and window support by version
platforms: [sqlite, python]
---
**Inspect first**
- `select sqlite_version();` - features below depend on it.
- `select name, type, sql from sqlite_master where type in ('table', 'view');` (`sqlite_schema` alias in 3.33+).
- `pragma table_info('t');` or `select * from pragma_table_info('t');` `pragma foreign_key_list('t');`
- CLI: `.tables`, `.schema t`, `.headers on`, `.mode column`. Open read-only: `sqlite3 -readonly f.db`.
- Python: `sqlite3.connect('file:f.db?mode=ro', uri=True)`.

**Dynamic typing (main source of wrong answers)**
- Declared types are only an affinity; any column can hold any value. Check real types: `select typeof(c), count(*) from t group by 1;`
- Numbers stored as TEXT compare as text: `'10' < '9'` is true; `max(c)` returns the lexically largest; integer/real values sort before any text.
- `text_col > 5` compares as text when the column has TEXT affinity. Cast explicitly: `cast(c as real) > 5`.
- `1` and `'1'` are different values in `distinct`/`group by` on a mixed column, and in `=`/joins unless one side has numeric affinity (expressions like `substr(c, 2)` have none). Normalise both join sides: `cast(a.id as integer) = cast(b.id as integer)` or `trim(a.code) = trim(b.code)`.
- CAST takes the longest numeric prefix and never errors: `cast('1,234' as integer) = 1`, `cast('$5' as real) = 0.0`, `cast('abc' as integer) = 0`. Clean first: `cast(replace(replace(c, ',', ''), '$', '') as real)`.
- Find non-numeric text: `select c, count(*) from t where c glob '*[^0-9.]*' group by c;` (also lists negatives; review them).
- `sum()`/`avg()` silently coerce text the same way; check `typeof` before aggregating.

**Numbers**
- Integer division truncates: `7/2 = 3`. Use `7 * 1.0 / 2` or `cast(a as real) / b`.
- Division by zero returns NULL (no error).
- `round(x, 2)` returns REAL, rounds half away from zero.

**Dates (no date type)**
- Stored as TEXT (`'YYYY-MM-DD HH:MM:SS'`), REAL (Julian day) or INTEGER (Unix epoch). Check samples and `typeof`.
- Date functions only understand ISO-8601 text, Julian day or epoch (with modifier): `date('03/07/2024')` is NULL.
- Reformat non-ISO text: `substr(s, 7, 4) || '-' || substr(s, 1, 2) || '-' || substr(s, 4, 2)` (for `MM/DD/YYYY`, zero-padded).
- Epoch: `datetime(ts, 'unixepoch')`; milliseconds: `datetime(ts / 1000, 'unixepoch')`.
- Modifiers: `date(d, '+1 month')`, `date(d, 'start of month')`, `date(d, 'weekday 0')` (Sunday on/after d), `date(d, '-6 days')`.
- Month arithmetic overflows: `date('2024-01-31', '+1 month') = '2024-03-02'`. For month buckets use `date(d, 'start of month', '+1 month')`.
- `strftime('%Y-%m', d)`, `%w` 0 = Sunday, `%W` week of year (Monday-start), `%s` epoch seconds, `%j` day of year.
- `strftime` returns TEXT: `strftime('%Y', d) = '2024'` (not `= 2024`).
- Day differences: `julianday(b) - julianday(a)` (fractional; wrap in `cast(... as integer)` or `round`).
- Text range filters: `d <= '2024-01-31'` excludes `'2024-01-31 10:00:00'`; use `d < '2024-02-01'` or `date(d) <= '2024-01-31'`.
- `'now'` is UTC; add `'localtime'` only if local time is intended.

**Joins and windows**
- RIGHT and FULL OUTER JOIN exist only in 3.39+. Older: emulate FULL OUTER:
```sql
select a.k, a.v, b.w from a left join b on a.k = b.k
union all
select b.k, null, b.w from b left join a on a.k = b.k where a.k is null;
```
- Window functions 3.25+; `filter (where ...)` on aggregates and `nulls first/last` 3.30+.
- NULLs sort FIRST in ASC by default.
- No `median`/`percentile_cont` built in: compute with `row_number()`/`count(*)` windows or in Python.

**Strings**
- `like` is case-insensitive for ASCII only; `glob` matches exact case, with `*`/`?` wildcards. `=` compares exact case unless `collate nocase`.
- No built-in `regexp` function: in Python, `con.create_function('regexp', 2, lambda p, s: s is not None and re.search(p, s) is not None)`, then `where c regexp 'pat'`.
- `group_concat(x, ',')` order is not guaranteed (ORDER BY inside the aggregate needs 3.44+).
- Trailing spaces are significant: `trim(c)` before comparing.

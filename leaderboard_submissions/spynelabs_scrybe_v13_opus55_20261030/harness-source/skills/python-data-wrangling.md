---
name: python-data-wrangling
description: pandas and duckdb-in-Python patterns - loading from databases, dtypes, checked merges, groupby, datetimes, deterministic scripts
platforms: [python]
---
**Loading**
- SQLite: `pd.read_sql_query(q, sqlite3.connect('file:f.db?mode=ro', uri=True))`.
- Postgres: `pd.read_sql_query(q, sqlalchemy.create_engine(url))` (plain DBAPI connections work but warn).
- DuckDB: `duckdb.connect('f.duckdb', read_only=True).sql(q).df()`.
- Snowflake: `cur.execute(q); cur.fetch_pandas_all()` (column names come back UPPER-case).
- Parameterise: `pd.read_sql_query("select * from t where id = ?", con, params=(5,))` (`%s` / `%(name)s` for psycopg).
- CSV: `pd.read_csv(f, dtype={"id": str})` to keep leading zeros.

**Inspect compactly**
```python
pd.set_option("display.width", 200, "display.max_columns", 50)
print(df.shape); print(df.dtypes.to_string())
print(df.head(5).to_string())
print(df.isna().sum().to_string())
print(df["col"].value_counts(dropna=False).head(20).to_string())
```
- `df.describe(include="all")` for ranges; print scalars, not whole frames.

**Dtypes**
- `object` columns may mix str/int/None. Check: `df.col.map(type).value_counts()`.
- To numbers: `pd.to_numeric(s.str.replace(r"[,$%\s]", "", regex=True), errors="coerce")`; count new NaNs and inspect the raw values that failed.
- Ints with missing values become float64 (`123.0`); `astype(str)` then yields `"123.0"`. Use `astype("Int64")` before converting ids to str.
- DB `Decimal` values arrive as object dtype: `astype(float)` before math.
- Merging int64 with object keys raises an error; cast both sides to the same type (usually `str`) first.

**Merges**
```python
m = left.merge(right, on="key", how="left", indicator=True, validate="many_to_one")
print(m["_merge"].value_counts())
assert len(m) == len(left)
```
- pandas MATCHES NaN keys to each other (SQL does not); drop or fill null keys before merging.
- Overlapping column names get `_x`/`_y`; set `suffixes=("", "_r")` or select columns first.

**groupby / agg**
- `groupby` drops NaN keys by default: pass `dropna=False` when NULL groups matter.
- Named aggregation: `df.groupby("k", as_index=False).agg(n=("id", "nunique"), total=("amt", "sum"), avg=("amt", "mean"))`.
- `count` / `nunique` / `mean` skip NaN; `size` counts rows. `sum` of all-NaN is 0, not NaN (use `min_count=1`).
- Ratio of sums: compute sums in `agg`, divide afterwards; do not `mean()` per-row ratios unless asked.

**Datetimes**
- Explicit format: `pd.to_datetime(s, format="%Y-%m-%d %H:%M:%S", errors="coerce", utc=True)`; mixed formats need per-format passes combined with `fillna`.
- Epoch: `pd.to_datetime(s, unit="s", utc=True)` (`unit="ms"` if values > 1e11).
- Local buckets: `s.dt.tz_convert("Europe/Berlin").dt.to_period("M")`; naive local data: `s.dt.tz_localize("Europe/Berlin")`.
- `s.dt.date`, `s.dt.dayofweek` (0 = Monday), `s.dt.isocalendar().week`.

**Safe mutation**
- Assign with `.loc`: `df.loc[df.amt < 0, "amt"] = 0`; never `df[df.a > 0]["b"] = 1` (chained assignment).
- Take `.copy()` when keeping a filtered subset you will modify: `sub = df[df.ok].copy()`.
- Prefer `df.assign(new=lambda d: d.a / d.b)` pipelines.

**DuckDB on DataFrames**
- `duckdb.sql("select k, sum(v) from df group by k").df()` - SQL over pandas without copying; handy for window functions and complex joins.

**Deterministic scripts**
- One script, top to bottom, from sources to final number; re-run it fully before reporting.
- Sort before `head`, `drop_duplicates`, `iloc[0]`: `sort_values(["k", "ts"], kind="mergesort")`.
- Fix seeds (`random_state=0`); do not depend on set iteration order.
- Print the final answer with its inputs (numerator, denominator, n) and round only at output: `print(f"{x:.2f}")`. Python `round` is half-to-even.

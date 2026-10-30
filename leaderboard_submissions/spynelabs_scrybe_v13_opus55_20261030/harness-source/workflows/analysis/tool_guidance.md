- `bash` with a python heredoc is the main tool:
  ```
  python3 - <<'PY'
  from multidb import q
  df = q("db_name", "SELECT ... FROM ...")                                       # SQL databases
  docs = q("mongo_db", {"collection": "c", "filter": {}, "projection": {"field": 1}})  # MongoDB
  ...
  print(answer)
  PY
  ```
  `q(db_name, query)` returns a pandas DataFrame (read-only); `db_name` is a logical database name from the task (also in the database config named in the start-up context; `multidb.dbs()` lists them). Postgres: double-quote mixed-case identifiers. Mongo: omit "limit" to get all documents; aggregations use `{"collection": "c", "pipeline": [...]}`. Push filters/aggregations into SQL when easy; otherwise do it in pandas. Save intermediate frames to `*.parquet` files in the working directory if you will reuse them in a later step.
- `dbq <db_name> "<query>" [--max N] [--save NAME]` runs one read-only query, prints a compact table and saves the full result to `results/NAME.parquet` in the working directory. `dbschema [db] [table]` lists tables/collections, row counts, columns and sample rows; `dbsnap` prints a compact snapshot of every database.
- `sl` shows the offline profile of the provided databases: tables, columns, types, row counts, null and distinct counts, sample values, and candidate keys computed from the profiled rows; it was computed before this task from the same data you can query (`sl tables`, `sl table <name>`, `sl column <table>.<col>`, `sl joins [<table>]`, `sl search <term>`, `sl help`).
- `python3` has pandas, numpy, duckdb; use what is installed (installing is not possible).
- `llm_label --input FILE --text "{col}" --instruction "..." --labels A,B,C` (or `--extract "..."`) labels or extracts a value for every row of a table with an LLM (cached; parallel). It writes a parquet file (default `<input>.labeled.parquet`) with a `label` column and prints the label distribution. Rows without a valid answer get label = null and a `label_status` saying why. Check a few labels by eye before using them.
- Long tool output is truncated: keep tool output small (counts, `head`, `LIMIT`).
- Do not open files or list directories outside the working directory and the database files: everything you need is in this prompt.

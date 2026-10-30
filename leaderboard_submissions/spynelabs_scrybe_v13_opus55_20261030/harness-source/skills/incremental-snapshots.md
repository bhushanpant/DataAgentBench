---
name: incremental-snapshots
description: Incremental models, merge strategies, snapshots/SCD2, late-arriving data
triggers: [type:incremental_snapshot, incremental, snapshot, scd, slowly changing, is_incremental, merge, unique_key, late-arriving, backfill]
---
- Incremental skeleton:
  ```sql
  {{ config(materialized='incremental', unique_key='id', incremental_strategy='merge', on_schema_change='append_new_columns') }}
  select ... from {{ ref('src') }}
  {% if is_incremental() %}
  where updated_at > (select coalesce(max(updated_at), '1900-01-01') from {{ this }})
  {% endif %}
  ```
  DuckDB supports `delete+insert`, `append`, and `merge` (dbt-duckdb ≥1.9 / duckdb ≥1.1); if unsure use `delete+insert` with `unique_key`. Snowflake supports `merge` (default), `delete+insert`, `append`.
- For late-arriving data use a lookback window: `where updated_at >= (select max(updated_at) - interval 3 day from {{ this }})` and dedupe with `qualify row_number() over (partition by id order by updated_at desc) = 1`.
- Test both paths: `dbt run -s m --full-refresh` then `dbt run -s m` again; row counts must agree and keys stay unique.
- When the task names late-arriving rows, corrections or restatements, exercise that path: after a first build, make a late or corrected row visible to the model (a test seed or `--vars` switch the model already supports, never edits to the source data), rerun incrementally, and check that the row is captured, keys stay unique and the incremental result equals a full refresh. Remove test-only switches you added before finishing.
- Snapshots (`snapshots/*.sql` or yml-defined in dbt ≥1.9):
  ```sql
  {% snapshot name_snapshot %}
  {{ config(target_schema='main', unique_key='id', strategy='timestamp', updated_at='updated_at') }}
  select * from {{ ref('x') }}
  {% endsnapshot %}
  ```
  `strategy='check', check_cols=[...]` when there is no reliable timestamp. Run with `dbt snapshot -s name_snapshot`. SCD2 columns: `dbt_valid_from`, `dbt_valid_to`, `dbt_scd_id`, `dbt_updated_at`.

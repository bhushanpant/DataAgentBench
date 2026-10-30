---
name: dbt-testing-and-docs
description: dbt tests, selectors, show/compile/ls, yml docs, freshness, seeds, snapshots and model contracts
platforms: [dbt]
---
**Generic tests in yml**
```yaml
models:
  - name: orders
    description: One row per order.
    columns:
      - name: order_id
        description: Primary key.
        data_tests: [unique, not_null]
      - name: status
        data_tests:
          - accepted_values:
              values: ['placed', 'shipped']
      - name: customer_id
        data_tests:
          - relationships:
              to: ref('customers')
              field: customer_id
```
- `data_tests:` is the dbt 1.8+ key; `tests:` still works. Newer versions also accept nesting test args under `arguments:`. Match the project's existing style.
- Test config: `config: {severity: warn, where: "created_at >= '2024-01-01'", error_if: ">10", store_failures: true}`.
- Model-level tests go under the model's `data_tests:` (e.g. multi-column uniqueness).

**dbt_utils tests** (needs `dbt_utils` in `packages.yml`, then `dbt deps`)
- `dbt_utils.unique_combination_of_columns: {combination_of_columns: [a, b]}`
- `dbt_utils.expression_is_true: {expression: "amount >= 0"}`
- `dbt_utils.accepted_range: {min_value: 0, max_value: 100}`
- `dbt_utils.not_null_proportion: {at_least: 0.95}`, `dbt_utils.equal_rowcount: {compare_model: ref('x')}`, `dbt_utils.recency`.

**Singular and custom tests**
- Singular: `tests/assert_no_negative_totals.sql` returning the FAILING rows: `select * from {{ ref('orders') }} where total < 0`.
- Custom generic: `tests/generic/is_positive.sql`:
  `{% test is_positive(model, column_name) %} select * from {{ model }} where {{ column_name }} <= 0 {% endtest %}`
- Debug a failure: run the compiled test SQL from `target/compiled/<project>/...` directly to see the offending rows.

**Unit tests (1.8+)**: `unit_tests:` with `model:`, `given: [{input: ref('x'), rows: [...]}]`, `expect: {rows: [...]}`; run with `dbt test -s test_type:unit`.

**build vs run + test**
- `dbt build` runs seeds, snapshots, models and tests in DAG order; a failing test SKIPS downstream nodes.
- `dbt run` then `dbt test` does not skip; use it when you need every model built regardless.

**Selection**
- `-s model` / `model+` (descendants) / `+model` (ancestors) / `2+model` (2 levels) / `@model` (descendants and their ancestors).
- `tag:nightly`, `path:models/staging`, `source:raw+`, `config.materialized:incremental`, `test_type:singular`, `--exclude ...`, `--resource-type model`.
- `state:modified+ --state <dir>` needs a manifest.json from a previous run in `<dir>`; `result:error+` needs run_results.json there.

**Inspection commands**
- `dbt ls -s model+ --resource-type model --output name` lists without running.
- `dbt compile -s model` writes SQL to `target/compiled/`; `target/run/` holds the executed DDL/DML.
- `dbt show -s model --limit 10` previews a model's query; `dbt show --inline "select count(*) from {{ ref('m') }}"`.
- `dbt parse` validates yml/Jinja quickly; `dbt debug` checks the connection; `dbt deps` installs packages.

**Docs**
- Every model/column: `description:`; reusable text via `{% docs name %}...{% enddocs %}` in a `.md` and `description: '{{ doc("name") }}'`.
- `dbt docs generate` builds `target/catalog.json` + `manifest.json` (fails if the warehouse is unreachable).

**Sources and freshness**
- On a source or table: `loaded_at_field: _loaded_at` plus `freshness: {warn_after: {count: 12, period: hour}, error_after: {count: 24, period: hour}}`.
- Run `dbt source freshness`; tables without `loaded_at_field` are skipped.

**Seeds**
- CSVs in `seeds/`; `dbt seed -s name` (`--full-refresh` after changing columns).
- Pin types in `dbt_project.yml` to keep ids/zip codes as text: `seeds: <project>: <seed>: +column_types: {zip: varchar(10)}` (nested keys).

**Snapshots**
- Strategies: `timestamp` (`updated_at: col`) or `check` (`check_cols: [a, b]` or `all`); require `unique_key`.
- Output adds `dbt_valid_from`, `dbt_valid_to` (NULL = current), `dbt_scd_id`; run with `dbt snapshot`.

**Contracts**
- `config: {contract: {enforced: true}}` on the model: EVERY column must be listed in yml with `name` and `data_type`, and the yml column set must match the model's output exactly.
- `constraints:` (`not_null`, `primary_key`, ...) are enforced or only documented depending on the platform.
- Contracts are not supported on ephemeral models; for incremental models `on_schema_change` must be `append_new_columns` or `fail`.
- After any change: `dbt build -s model` and read every test result; report pre-existing failures separately.

---
name: dbt-projects
description: Working in an existing dbt project - packages, lineage, vars, yml and materializations
inline: true
---
- Installed packages (`dbt_packages/`, listed in `packages.yml`) often already model the same entities as the task. Before writing logic for such an entity, read the package's models, macros and yml docs and reuse its definitions and column semantics. Do not edit files under `dbt_packages/`; to change a package model, disable it in `dbt_project.yml` (`models: <package>: <model>: +enabled: false`) and add a project model of the same name, or set the package's vars.
- Source identifiers and schemas are often mapped through vars in `dbt_project.yml` (`var('...')`); follow those instead of hard-coding relation names. Use `ref()`/`source()` for every relation.
- Before removing, renaming or hiding a column or model, find every use: `dbt ls -s <model>+ --resource-type model` for downstream models, and `grep -rn "<column>" models macros snapshots` (plus yml files) for references. Change each one and the yml entries that document or test it.
- A model controlled by a var or flag (`{% if var('x', ...) %}`) has two code paths; build the affected models with each value (`--vars '{x: true}'`) as well as the configured one.
- Look for existing helpers before writing your own: project `macros/`, and package macros such as `dbt_utils.generate_surrogate_key`, `dbt_utils.date_spine`, `dbt_utils.star`.
- Incremental models keep old rows: after changing their logic, rebuild with `--full-refresh`. Ephemeral models have no relation in the warehouse; query their compiled SQL instead.
- Adding or changing a column: keep the model's yml (description, tests) consistent, then `dbt build -s <model>` to run its tests. A test failure that existed before your change is worth naming in the summary.
- Views and tables read upstream relations as they are in the warehouse; when an upstream model changed, build it too (`dbt build -s +<model>` restricted to what you changed).
- Before writing a status, flag or validity filter, look at how existing models in the project filter the same column (`grep -rn "<column>" models | head`) and reuse their predicate and value spellings (for example mixed-case or abbreviated status codes), unless the task defines the filter itself.
- Column names: when sources have upper-case column names, alias output columns to exactly the names the task gives (`"ORDER_ID" as order_id`); match the requested order when the task lists columns.
- After building, check where each model landed and how (`sql "select table_schema, table_name, table_type from information_schema.tables where table_name ilike '<model>'"`): folder configs keyed under a different project name in `dbt_project.yml` are silently ignored, so the model may be a view in the default schema.

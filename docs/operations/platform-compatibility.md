# Touchstone v1 local compatibility record

Checked 2026-09-26 on Python 3.12.13, macOS arm64. The uv workspace lock resolves all
Reckoner and Touchstone packages together. Direct platform pins are in
`platform/pyproject.toml`; all transitive versions and artifact hashes are in `uv.lock`.

| Component | Resolved version | Check |
| --- | --- | --- |
| dbt Core | 1.12.5 | Disposable DuckDB `dbt build` |
| dbt DuckDB adapter | 1.11.0 | Same build |
| DuckDB engine | 1.5.5 | Locked adapter dependency |
| dbt MetricFlow CLI | 0.15.0 | `mf query` returned `3.75` |
| MetricFlow engine | 0.213.0 | Locked CLI dependency |
| dbt Snowflake adapter | 1.12.1 | Installed and resolved; no Snowflake connection |
| Dagster / webserver | 1.13.24 | Installed and resolved |
| ClickHouse Connect | 1.9.0 | Installed and resolved |
| Elementary dbt package | 0.25.1 | dbt package installed, hooks and schema test passed |

The registry references for the next collector/storage task are pinned to multi-platform
manifest digests:

```text
otel/opentelemetry-collector-contrib:0.136.0@sha256:45392d534c1edcc809c2d112394029246bc679d2ae5ea7081414a1fc74f2c621
clickhouse/clickhouse-server:25.8@sha256:0152dd511befe6a2c2ef53e930726179669b08116da78500b37c51c96ff5ee77
```

These are registry manifest checks, not local container startup checks. The ClickHouse
`25.8` tag is a release series, so the digest is the actual immutable selection. Snowflake
live operation remains an owner access dependency; DuckDB output is a local preview.

## Reproduction

With `UV_PYTHON_INSTALL_DIR=/private/tmp/touchstone-uv-python` and
`UV_CACHE_DIR=/private/tmp/touchstone-uv-cache` set:

```sh
uv lock
uv sync --frozen --all-packages
uv run --frozen --all-packages pytest contracts/tests/test_run_declarations.py platform/tests/test_contracts.py -q
docker buildx imagetools inspect otel/opentelemetry-collector-contrib:0.136.0
docker buildx imagetools inspect clickhouse/clickhouse-server:25.8
```

For the disposable fixture under `/private/tmp/touchstone-compat-fixture`, `packages.yml`
declared `elementary-data/elementary` version `0.25.1`; `profiles.yml` selected a local
DuckDB file. The project had two seed rows with amounts 1.25 and 2.50, a typed model, a
daily time spine, one simple `revenue` semantic metric, and an
`elementary.schema_changes` data test. Commands and observed results:

```text
dbt deps --project-dir <fixture> --profiles-dir <fixture>
  Installed elementary-data/elementary 0.25.1 and dbt-labs/dbt_utils 1.4.1
dbt build --project-dir <fixture> --profiles-dir <fixture> --select orders_seed orders time_spine_daily
  PASS=5 WARN=0 ERROR=0; Elementary start/end hooks passed
dbt build --project-dir <fixture> --profiles-dir <fixture> --select orders+ elementary
  PASS=34 WARN=0 ERROR=0; elementary_schema_changes_orders_ passed
DBT_PROFILES_DIR=<fixture> mf query --metrics revenue --decimals 2
  revenue = 3.75
```

The original `dbt build` first failed parsing because MetricFlow requires a daily time
spine. Adding the documented time spine made the fixture pass. The workspace lock changed
Reckoner's transitive `certifi` from 2026.7.22 to 2025.1.31 and `filelock` from 4.0.3 to
3.32.7 under the shared resolver; the nonintegration
suite is rerun after this lock change. No paid provider or Snowflake call was made.

Primary references: [dbt MetricFlow time spine](https://docs.getdbt.com/docs/build/metricflow-time-spine),
[dbt MetricFlow release metadata](https://pypi.org/project/dbt-metricflow/0.15.0/),
[dbt DuckDB adapter](https://github.com/duckdb/dbt-duckdb),
[Elementary package registry](https://hub.getdbt.com/elementary-data/elementary/latest/),
[Elementary schema change test](https://docs.elementary-data.com/data-tests/schema-tests/schema-changes).

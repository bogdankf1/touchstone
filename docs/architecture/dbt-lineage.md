# Local dbt lineage

Generated on 2026-09-28 from the final non-root platform image with `dbt parse` (dbt 1.12.5, DuckDB adapter 1.11.0, pinned packages). The generated manifest is retained locally as ignored `artifacts/phase2/dbt-lineage-manifest.json` with SHA-256 `087923ee1c5e338b3b5f29e361f512c75d8a2a3348015a8f9649c179b380c879`. This table is extracted from its `depends_on.nodes`, not inferred from filenames. The manifest contains ten Touchstone models, seven Touchstone tests and two semantic models (`touchstone_runs`, `touchstone_contributions`).

| Model | Direct model/source dependencies |
|---|---|
| `stg_events` | `raw_measurements` |
| `int_calls` | `stg_events` |
| `int_evaluations` | `stg_events` |
| `int_outcomes` | `stg_events` |
| `int_tasks` | `raw_declarations`, `stg_events` |
| `mart_contributions` | `stg_events` |
| `mart_nodes` | `int_calls` |
| `mart_runs` | `raw_declarations`, `raw_rejections`, `stg_events`, `int_calls`, `int_tasks`, `int_outcomes`, `int_evaluations` |
| `mart_contribution_rates` | `raw_declarations`, `mart_contributions`, `mart_runs` |
| `time_spine_daily` | no direct dependency |

`mart_runs` is the completeness and cost publication boundary; `mart_contribution_rates` attaches versioned workload-supplied numerators and denominators to its complete runs. The API reads published marts from the immutable DuckDB generation. This lineage is a parse-time dependency graph; the live refresh and reconciliation evidence separately verifies materialization, tests and values. No Snowflake execution is implied.

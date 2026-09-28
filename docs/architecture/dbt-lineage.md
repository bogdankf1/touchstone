# Local dbt lineage

Generated on 2026-09-28 from the final-review dbt build against a copy of the measured warehouse (dbt 1.12.5, DuckDB adapter 1.11.0, pinned packages, 2 GB and one thread). The generated manifest is retained locally as ignored `artifacts/phase2/dbt-lineage-final-manifest.json` with SHA-256 `79180dc58670e8246c4c70d266551231da1da666d062c04b966bafaede394809`. This table is extracted from its `depends_on.nodes`, not inferred from filenames. The manifest contains 12 Touchstone models, 7 Touchstone tests and 2 semantic models (`touchstone_runs`, `touchstone_contributions`).

| Model | Direct model/source dependencies |
|---|---|
| `stg_events` | `raw_measurements` |
| `int_calls` | `stg_events` |
| `int_evaluations` | `stg_events` |
| `int_tasks` | `raw_declarations`, `stg_events` |
| `int_event_compat` | `raw_declarations`, `stg_events` |
| `int_task_membership` | `stg_events`, `int_tasks` |
| `int_outcomes` | `stg_events`, `int_event_compat` |
| `mart_contributions` | `stg_events`, `int_event_compat` |
| `mart_nodes` | `int_calls`, `int_task_membership` |
| `mart_runs` | `raw_rejections`, `raw_declarations`, `stg_events`, `int_calls`, `int_task_membership`, `int_event_compat`, `int_tasks`, `int_outcomes`, `int_evaluations` |
| `mart_contribution_rates` | `raw_declarations`, `mart_contributions`, `mart_runs` |
| `time_spine_daily` | no direct dependency |

`mart_runs` is the completeness and cost publication boundary. `int_task_membership` maps valid descendants to declared roots, while `int_event_compat` checks event versions against declarations. `mart_contribution_rates` publishes eligible rates from independently complete, compatible contribution populations; unrelated missing outcomes do not erase their observed components. The API reads published marts from the immutable DuckDB generation. This graph records build-time dependencies; the reconciliation evidence separately verifies values. No Snowflake execution is implied.

# Inputs and access needed from the owner

Updated: 2026-09-26. This checklist records dependencies, not authorization to
provision paid services. Keep secret values outside Git and chat.

| Dependency | Current status | Needed from the owner | Gate affected |
|---|---|---|---|
| Snowflake | Deferred explicitly; use DuckDB for Phase 2 | Account identifier, authentication configured privately, approved role/database/schema/warehouse, confirmation of credits and spending limit | Snowflake integration and demonstrated warehouse-backed dashboard; does not block local Phase 2 |
| Jev | Waiting for access; no development stub or substitute scorer approved | API access, credentials configured privately, available API documentation, model/version, rate limits and pricing/budget confirmation | Phase 3 scoring integration and calibration |
| Anthropic | Phase 1 measured access worked; current credentials are not revalidated | Renew access only if it stops working; approve a concrete paid evaluation or generation run before additional spending | Later case-note generation and DeepEval/Ragas judge runs; no new calls needed for Phase 2 baseline replay |
| GitHub publishing | Owner has handled pushes and merges | Push reviewed branches and merge after checks pass, unless publishing responsibility is explicitly changed | Publishing completed phases; local implementation is independent |
| Phase 2 design and plan | Specification approved on 2026-09-26; plan prepared for review | Review the concrete implementation plan | Phase 2 implementation |

## Details to settle when access becomes available

For Snowflake, use the smallest suitable warehouse, 60-second auto-suspend,
scheduled transformations and the approved service budget. Confirm trial expiry
and post-trial cost before use. Verify staging, dbt/MetricFlow compatibility,
permissions and reconciliation against the same DuckDB fixtures. Do not mark the
Snowflake demonstration complete based on local tests.

For Jev, verify authentication and request/response semantics before building the
integration. Confirm supported probability semantics, model identity and rate
limits. Calibration uses separate development data; the frozen baseline cohort
must not become a tuning set.

For paid Anthropic evaluations, present the fixture count, model, cost bound and
remaining shared budget first. Phase 1 ended with 1,040 settled calls costing
USD 0.493151 against the initial USD 10 limit; remaining balance alone does not
authorize additional calls. Never repeat baseline generation merely to ingest its
existing traces.

No further dataset download, currency/timezone decision, tenant definition or
baseline size decision is currently needed from the owner. The existing choices
remain in force. If local resource measurements reveal a capacity problem, record
the measured limit and proposed adjustment here rather than silently changing
scope.

Update this checklist at each phase handoff. Close an access item only after its
integration is verified, not merely after credentials are supplied.

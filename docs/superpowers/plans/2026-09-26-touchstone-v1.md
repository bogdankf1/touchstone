# Touchstone v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reproduce the frozen baseline through an independent local measurement platform and display it alongside a clearly synthetic second workflow.

**Architecture:** OTLP Collector writes append-only ClickHouse traces. Dagster stages validated generic events into an unpublished DuckDB snapshot, runs dbt/MetricFlow/Elementary checks, and publishes only verified snapshots. FastAPI and Next.js read published results; Snowflake integration remains pending access.

**Tech Stack:** Python 3.12, uv, FastAPI, OTel Collector Contrib, ClickHouse, DuckDB, dbt, MetricFlow, Elementary, Dagster, Next.js/TypeScript, Compose and kind.

**Spec:** `docs/spec/003-touchstone-v1.md`, approved 2026-09-26.

**Status:** Approved by the owner on 2026-09-26; execution started.

## Global constraints

- Start from merged PR #3 on `main`; local recorded merge is `cbdbaea`. Verify the actual remote before execution. Preserve existing worktrees and ignored artifacts.
- Use a new `feat/phase-2-touchstone-v1` branch and `.worktrees/phase-2-touchstone-v1` worktree after approval. Bring the approved documents into it without losing the originals.
- Touchstone consumes only OTLP. No imports of Reckoner, operational database reads, report-derived dashboard values or label-based platform formulas.
- All persisted application/event rows are tenant-scoped. Operator-level refresh receipts contain no cross-tenant application payload; per-run refresh records retain tenant identity.
- No paid provider calls, Snowflake provisioning or Jev substitutes. Preserve the existing USD 0.493151 lifetime spend and Phase 1 ledger/exports/backups.
- DuckDB results are labelled local preview; Snowflake live verification stays on the owner-dependency checklist.
- Exact decimal money, null for undefined metrics, explicit populations and completeness, and separate fabricated measurements from measured calls on simulated data.
- Each task gets a fresh implementing subagent, TDD, self-review, independent review and a focused commit. Keep tasks sequential where their interfaces depend on earlier work.
- Maintain existing Phase 1 tests and CI. Never add provider credentials to CI. Owner pushes and merges.

## Review focus

1. Same event identity with changed contents must invalidate affected results instead of arbitrarily selecting one (Tasks 1–3).
2. Equal ingestion timestamps, late inserts and restarted extraction must not skip events (Task 2).
3. API readers during refresh or process death must see one verified generation, not partial or mixed results (Tasks 3–4).
4. Tenant/run identifiers containing quotes, path separators or HTML must stay data, not SQL, filenames or markup (Tasks 1, 4–5).
5. All observed tasks succeeding does not establish completeness without declared expectations, including evaluation expectations (Tasks 1, 3, 5).

## File map and shared interfaces

- `platform/pyproject.toml`, `platform/src/touchstone_platform/`: independently installable Python package. Ship shared schemas as package resources; do not depend on Reckoner's resource loader.
- `platform/collector/config.yaml`, `platform/clickhouse/init.sql`: raw ingestion configuration and receipt-time/index setup, validated against the chosen exporter version.
- `platform/src/touchstone_platform/{contracts,extract,staging,refresh,api,query,cli}.py`: envelope validation, extraction, snapshot staging, publication, HTTP API, read queries and commands.
- `platform/src/touchstone_platform/orchestration/definitions.py`: Dagster assets/job/schedule.
- `platform/dbt/`: project, DuckDB profile template, sources, models, semantic definitions, Elementary dependency, assertions and adapter-dispatched SQL where needed.
- `workloads/synthetic/`: standalone emitter package using shared schemas and OTLP libraries, never platform or Reckoner internals.
- `web/`: Next.js application, server-side API client, dashboard and browser tests.
- `infra/compose.platform.yaml`, `infra/k8s/platform/`, `infra/Dockerfile.platform`, `infra/Dockerfile.web`: independent Phase 2 deployments.

Interface records are frozen by Task 1: `RunKey(tenant_id, workflow_id, run_id)`, `ValidatedEvent(document, content_sha256, received_at)`, `RejectedEvent(reason, safe_identity)`, `RefreshResult(generation, status, published_at, counts, errors)`. Full sensitive rejected payloads are not returned through the API. API amounts are decimal strings; counts are integers; rates expose numerator/denominator and optional value.

## Task 1 — Validated measurement and run-declaration contracts

**Files:** create `contracts/schemas/run-declaration-v1.schema.json`, `contracts/examples/run-declaration-v1.json`, `contracts/tests/test_run_declarations.py`, `platform/pyproject.toml`, `platform/src/touchstone_platform/{__init__,contracts,resources}.py`, `platform/tests/test_contracts.py`; modify `pyproject.toml`, `uv.lock`, `contracts/otel-conventions.md`; create `docs/operations/platform-compatibility.md`.

**Interfaces:** `validate_event(document: dict, received_at: str) -> ValidatedEvent`; `validate_declaration(document: dict) -> dict`. Declarations use named span event `touchstone.run.declaration` with `touchstone.run.json`. Include schema/event/tenant/workflow/run identities, expected distinct task IDs and count, experiment versions, measurement mode, dataset-simulated flag, replay provenance and per-suite required checks/expected case IDs. Declarations contain expectations, not results. Hash identity canonically; reject conflicting declaration versions for the same run.

- [ ] Check official release metadata and solve a Python 3.12 dependency set including existing workspace dependencies. Pin the resolved lockfile and collector/ClickHouse images by digest. Verify actual dbt/MetricFlow DuckDB execution and Elementary package support in a tiny disposable fixture. Record exact versions and commands; stop for a concrete design change if no compatible set exists.
- [ ] Write `test_declaration_count_matches_unique_task_ids`, `test_conflicting_declarations`, `test_measurement_envelope_context_matches_span`, `test_unknown_schema_is_rejected` and `test_tenant_identity_is_not_a_path`. Assert count 2 with IDs `["a", "b"]` passes; duplicate IDs, mismatched count, missing tenant and span/envelope disagreement fail.
- [ ] Run `uv run --all-packages pytest contracts/tests/test_run_declarations.py platform/tests/test_contracts.py -q`; observe failures caused by missing behavior.
- [ ] Implement the smallest validators and immutable identity records. Preserve `measurement-v1` unchanged. Package the shared schemas independently. Add the workspace member and test path.
- [ ] Rerun the focused tests and Ruff; verify an installed platform wheel validates an event outside the checkout. Commit `feat: define platform ingestion contracts` after independent review.

## Task 2 — Durable OTLP raw ingestion and replay

**Files:** create `platform/collector/config.yaml`, `platform/clickhouse/init.sql`, `platform/src/touchstone_platform/{extract,cli}.py`, `platform/tests/{test_extract,test_replay,test_ingestion_integration}.py`, `infra/compose.platform.yaml`, `infra/smoke-platform-ingestion.sh`; extend `contracts/otel-conventions.md`.

**Interfaces:** `iter_measurements(client, *, through: datetime) -> Iterator[ValidatedEvent | RejectedEvent]`; `replay_exports(manifest_dir: Path, endpoint: str) -> ReplayResult(sent, pending, rejected_spans)` exposed as `touchstone replay --manifest-dir PATH --endpoint URL`. Extractor includes declarations. Replay validates all checksums and confined relative paths before sending any bytes.

- [ ] Write tests for identical replay, modified bytes, path escape, malformed/partially rejected OTLP responses, mismatched tenant attributes, old occurrence time arriving today, equal receipt timestamps, and extractor restart.
- [ ] Run `uv run --all-packages pytest platform/tests/test_extract.py platform/tests/test_replay.py -q`; confirm red.
- [ ] Configure Collector OTLP/HTTP, batching, bounded memory and persistent file-backed exporter queue. Use ClickHouse exporter trace storage with server-side receipt timestamp; verify actual nested event preservation with the pinned image. Disable expiry during acceptance and document retention before enabling it.
- [ ] Implement bounded paged extraction. For this small phase, rebuild from all raw receipts through a fixed cutoff each refresh, using stable keyset pagination and processing every timestamp tie. Full bounded replay avoids silently missing late inserts at a watermark; record cutoff and counts. Do not introduce incremental checkpoint complexity until needed.
- [ ] Run focused tests, then `bash infra/smoke-platform-ingestion.sh`: ingest fixture, stop/restart the disposable collector/ClickHouse, verify delivery and full replay without lost events. Raw duplicates are allowed; assert logical equality after validation. Commit after review.

## Task 3 — Governed metrics and atomic scheduled publication

**Files:** create `platform/src/touchstone_platform/{staging,refresh}.py`, `platform/src/touchstone_platform/orchestration/definitions.py`, `platform/dbt/{dbt_project.yml,profiles.yml.example,packages.yml}`, `platform/dbt/models/{sources.yml,stg_events.sql,int_tasks.sql,int_calls.sql,int_outcomes.sql,int_evaluations.sql,mart_runs.sql,mart_nodes.sql,mart_contributions.sql,semantic_models.yml}`, `platform/dbt/tests/`, `platform/tests/{test_metrics,test_refresh,test_semantics}.py`.

**Interfaces:** `build_snapshot(events, declarations, target: Path) -> StagingReceipt`; `refresh(settings: Settings) -> RefreshResult`; command `touchstone refresh`. `Settings` in `platform/src/touchstone_platform/settings.py` contains ClickHouse connection, warehouse directory, 15-minute schedule and resource limits. Publish immutable generation filenames via atomic `current.json`, with separate atomic `refresh-status.json` for latest failure/attempt status.

- [ ] Write fixtures and assertions: model costs `0.1 + 0.2 = Decimal('0.3')`; two expected tasks, one correct and review/error costs 4/6 gives CPST `10.3`; zero correct gives null. Duplicate events/calls do not increase cost; incompatible currencies/configs and conflicting results yield incomplete metrics, never a synthetic sum.
- [ ] Add tests for 100 latency samples `[1..100]` giving nearest-rank p99 `99`, tenant-filtered percentiles, repeated attempts counted once as a logical task but with all distinct call costs, missing expected cases and all required checks per case. Root-task latency uses first start to terminal completion when multiple attempts exist; child spans do not become extra tasks.
- [ ] Run `uv run --all-packages pytest platform/tests/test_metrics.py platform/tests/test_semantics.py -q`; confirm red.
- [ ] Implement normalized staging and dbt models. Use DECIMAL(38,12) cost components with overflow/scale rejection; retain exact numerator/denominator and compute displayed CPST with Python Decimal precision 28 when warehouse division would produce a float. MetricFlow supplies governed component/count/rate queries; do not maintain an alternate Python aggregation engine. Test component queries against marts.
- [ ] Add Elementary checks and a tenant-scoped quality receipt. Define generic contribution deduplication by task/metric/definition, preserve nulls, and record conflicting outcome/evaluation revisions as incomplete. Declaration case membership, not count alone, determines coverage.
- [ ] Write `test_reader_keeps_old_generation_after_failed_build`, `test_crash_before_publish`, `test_overlapping_refresh_is_rejected` and `test_single_response_uses_one_generation`. Confirm red, then implement exclusive refresh lock, separate working database, sequential dbt/MetricFlow checks, closed connections before publication, and atomic manifest replacement. Keep prior generations during Phase 2; no unsafe automatic deletion of active-reader files.
- [ ] Implement Dagster assets and disabled-by-default 15-minute schedule, enabled explicitly by deployment. Run the dbt build/tests, semantic queries, Elementary checks and refresh tests; confirm failure cannot advance the published generation. Commit after review.

## Task 4 — Read API with trustworthy freshness and evidence

**Files:** create `platform/src/touchstone_platform/{api,query}.py`, `platform/tests/{test_api,test_query}.py`, `contracts/schemas/dashboard-response-v1.schema.json`; extend platform CLI.

**Interfaces:** `create_app(settings: Settings) -> FastAPI`; `open_snapshot(settings) -> SnapshotReader` resolves the published generation once per request. Read-only endpoints: `/healthz`, `/readyz`, `/v1/workflows`, `/v1/runs`, `/v1/runs/{run_id}/summary`, `/v1/runs/{run_id}/tasks`, `/v1/traces/{trace_id}`. Run/detail endpoints require workflow and tenant filters; explicit aggregate mode combines only compatible declared runs and retains tenant breakdowns. Paginated task/trace responses cap page size at 100.

- [ ] Write API tests: missing snapshot gives 503 readiness with a safe reason; no matching run gives 404; invalid filters give 422; unknown values are null; money is a decimal string. Trace lookup cannot return another tenant's events. Quotes and path separators in IDs never change SQL or filesystem selection.
- [ ] Run `uv run --all-packages pytest platform/tests/test_api.py platform/tests/test_query.py -q`; confirm red.
- [ ] Implement parameterized read queries over the published snapshot. Return metric definition versions, exact components/denominators, missing/conflict counts, generation, refresh times, latest refresh failure and measured/synthetic flags. Never expose connection strings, prompts or response text. This is local scoping, not an authentication/RBAC claim.
- [ ] Run focused tests including a reader concurrent with refresh and schema validation of each endpoint. Commit after review.

## Task 5 — Baseline dashboard in Next.js

**Files:** create `web/package.json`, `web/package-lock.json`, `web/tsconfig.json`, `web/eslint.config.mjs`, `web/.prettierrc.json`, `web/app/{layout.tsx,page.tsx,globals.css}`, `web/lib/{api.ts,types.ts}`, `web/components/{run-filters,metric-summary,cost-table,evaluation-table,task-table,refresh-state}.tsx`, `web/tests/dashboard.spec.ts`, `web/playwright.config.ts`, `infra/Dockerfile.web`.

**Interfaces:** server-side `getRunSummary(filters): Promise<DashboardResponse>` and matching run/task/trace clients; API location comes from server-only environment configuration. URL query parameters retain workflow, run and tenant selection. Use npm with a committed lockfile; verify a supported Node/Next combination before pinning.

- [ ] Write Playwright tests against controlled API fixtures: baseline 892/1000 correctness with 94/100 missed fraud visible, exact cost components, synthetic banner, empty/no snapshot, stale refresh, missing outcomes, filtered tenant totals and task evidence navigation. Assert keyboard focus and accessible labels; escape hostile identifier text.
- [ ] Run `npm --prefix web test -- --project=chromium`; confirm the behavioral tests fail before creating the page/components.
- [ ] Implement the dense dashboard with summary, component/node costs, generic contribution rates, evaluations and paginated task/evidence details. Use explicit local-preview and simulated-data labels. Separate USD 0.458940 provider cost from USD 7321.795940 modeled total. Display no improvement delta without a compatible comparison.
- [ ] Run frontend lint, formatting, TypeScript, production build and browser tests. Inspect desktop and narrow layouts using actual screenshots; fix clipping, focus and empty-state defects. Commit after review.

## Task 6 — Independent synthetic workflow and evaluation transport

**Files:** create `workloads/synthetic/pyproject.toml`, `workloads/synthetic/src/touchstone_synthetic/{__init__,cli,events}.py`, `workloads/synthetic/tests/test_workflow.py`, `platform/tests/test_workflow_independence.py`; modify root workspace and lockfile.

**Interfaces:** `touchstone-synthetic emit --endpoint URL --run-id ID`; deterministic logical IDs, tenant-specific declarations and schema-valid OTLP. Fixed fixture: four tasks split 2/2 between tenants, three completed and one failed; two correct; fabricated online costs total USD 0.10, review cost USD 4, error cost USD 2; CPST USD 3.05. Include two named nodes and pass/fail/error evaluation cases.

- [ ] Write tests asserting exact expected amounts/counts, repeatable logical identities, all `simulated=true`, no provider credentials required and no Reckoner/platform implementation imports.
- [ ] Run `uv run --all-packages pytest workloads/synthetic/tests platform/tests/test_workflow_independence.py -q`; confirm red.
- [ ] Implement emitter using shared contracts and OTel only. Add distinctly synthetic case-note evaluation transport fixtures with judge/version/reference fields; do not install or invoke paid judge clients merely to populate the dashboard.
- [ ] Run emitter through the real collector, refresh warehouse and verify API/UI expected values alongside Reckoner. Include a wrong/missing required-check case that remains incomplete. Commit after review.

## Task 7 — Deployment, reconciliation and phase evidence

**Files:** create `infra/Dockerfile.platform`, `infra/k8s/platform/{namespace,clickhouse,collector,warehouse,api,web,smoke-job}.yaml`, `infra/smoke-platform.sh`, `platform/tests/test_measured_reconciliation.py`, `docs/operations/platform-runbook.md`, `docs/evidence/phase-2-touchstone.md`; modify `.github/workflows/ci.yaml`, architecture DSL, README and owner dependency checklist. Add `platform/dbt/profiles.snowflake.yml.example` with environment references only.

**Interfaces:** `touchstone verify --expected PATH --run-id ID` compares published API/warehouse results to an independent expected receipt and exits nonzero on mismatch. The expected receipt is used only by acceptance verification, never staging or metric calculation.

- [ ] Write reconciliation tests for aggregate and tenant values from the approved spec and Phase 1 evidence. Assert baseline 1000 completed, 892 correct, model `0.458940`, review `96`, error `7225.337`, CPST `8.208291412556053811659192825`; p99 within `0.000001` ms of `1315.1606670003275`. Check 14/900, 94/100 and 24/1000 contributions.
- [ ] Run fixture reconciliation tests red, then implement verifier and deployment commands. CI runs Python, dbt/MetricFlow/Elementary, frontend and a fabricated OTLP-to-dashboard smoke without provider credentials. Keep existing Postgres/Phase 1 jobs intact and separate integration markers to avoid requiring ClickHouse in the Postgres-only job.
- [ ] Build pinned non-root application images. Use a dedicated Phase 2 Compose project and separate kind namespace/cluster. Initial limits: ClickHouse 2 GiB, collector 256 MiB, refresh worker 2 GiB, API 512 MiB, web 512 MiB and Dagster control services combined 768 MiB. Build workloads separately from measurement; measure actual memory/disk and adjust within 8 GB, documenting changes.
- [ ] Verify source checksums; mount existing Phase 1 exports read-only. Prepare declaration sidecars from frozen task membership and known suite expectations, not calculated result values. Replay original failed pilot, revised pilot and baseline bytes without provider access. Check baseline logical 3000 spans/10000 measurement events separately from declarations, then replay again and prove no logical inflation. Check each pilot remains a separate run.
- [ ] Verify restart recovery, synthetic workflow, API and browser under Compose; then stop Compose and run kind smoke. Preserve Phase 1 volumes and old images. Record actual resource samples, backup/restore verification scope and shutdown commands. Snowflake example is configuration-only until access; do not claim live parity.
- [ ] Run the full relevant checks once on final code, collect browser screenshots, update Structurizr and generated dbt lineage, and perform archify drift validation. Document any unavailable Snowflake evidence in the owner checklist.
- [ ] Independent whole-branch review; fix confirmed findings with targeted tests. Finish with clean commits and local branch handoff for owner push. No automatic merge or Phase 3 work.

## Primary compatibility references

- Collector ClickHouse exporter: https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/clickhouseexporter
- MetricFlow packaging/adapters: https://github.com/dbt-labs/metricflow/tree/main/dbt-metricflow
- dbt DuckDB adapter: https://github.com/duckdb/dbt-duckdb
- DuckDB concurrency: https://duckdb.org/docs/current/connect/concurrency
- Elementary dependency metadata: https://github.com/elementary-data/elementary/blob/master/pyproject.toml

These references establish implementation constraints, not a verified version combination. Task 1 records the tested lockfile and exact image digests before dependent work begins.

## Self-review and handoff

Coverage: spec sections 1–4 → Tasks 1–2; metrics/publication → Task 3; API/dashboard → Tasks 4–5; synthetic/evaluations → Task 6; deployments/reconciliation/access gates → Task 7. Review-focus cases are assigned above. No schema or dependency has been changed during planning.

Execution method is already specified by the project: subagent-driven, with task and whole-branch reviews. Approval of this concrete plan is the remaining gate before creating the Phase 2 worktree and beginning Task 1.

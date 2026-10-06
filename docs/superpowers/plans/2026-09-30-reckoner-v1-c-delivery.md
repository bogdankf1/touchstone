# Reckoner v1 — Measurement and Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile Reckoner v1 behavior through Touchstone, compare retrieval arms, verify local delivery, and produce bounded experiment protocols and reviewable evidence.

**Architecture:** Reckoner persists generic measurement envelopes in a transactional outbox and exports via OTLP. Touchstone consumes those events independently into ClickHouse and DuckDB. Reports reconcile against separate expectations, while paid run protocols remain explicit owner gates.

**Tech Stack:** Existing OpenTelemetry/ClickHouse/Dagster/dbt/MetricFlow/Elementary/DuckDB and FastAPI; Python; Neo4j/GDS; Compose and local kind; existing Next.js dashboard.

**Spec:** [Approved Phase 3 specification](../../spec/004-reckoner-v1.md).

## Global Constraints

All [parent-plan constraints and gates](2026-09-30-reckoner-v1.md) apply. This
stage implements Tasks 10–13 on the shared branch after stages A/B. No Snowflake,
cloud provisioning, network publication, destructive cleanup, or paid calls are
implicit. The owner handles push and merge. Preserve every measured Phase 1/2
artifact, volume, branch, worktree, backup, and source file.

## Review Focus

Parent risks 1, 2, and 4 map to Tasks 10–13. Tests cover event retries/late arrivals,
reconciliation drift, delayed note costs, generation consistency, budget protocols
that exceed balances, and incomplete paid evaluations.

## Task 10 — Generic OTLP export and dashboard integration

**Files:** create `workloads/reckoner/src/reckoner/v1/telemetry/{__init__,events,outbox,exporter}.py`,
`workloads/reckoner/tests/{test_v1_measurement_events,test_v1_outbox}.py`,
`platform/tests/test_reckoner_v1_replay.py`,
`workloads/reckoner/expectations/reckoner-v1-local.json`; modify
`contracts/schemas/measurement-v1.schema.json` only additively if needed,
`contracts/otel-conventions.md`, Reckoner migrations/repository, and focused platform
extract/dbt/API tests when a generic bug is demonstrated.

**Interfaces:** `measurement_events(run: dict, task: dict, outcomes: list[dict]) -> list[dict]`;
`enqueue_events(repo: V1Repository, events: list[dict]) -> int`;
`export_pending(repo: V1Repository, exporter: OTLPExporter, *, limit: int) -> dict`;
`reconcile_report(local_report: dict, warehouse_report: dict, expected: dict) -> dict`.
Only generic `measurement-v1` and run-declaration documents cross into Touchstone.

- [ ] Write RED schema tests for execution, provider usage, outcome, evaluation,
  review, and domain metric contributions; pin generic identities/reproducibility,
  nullable results, currency, simulated provenance, and no fraud formula dependency.
- [ ] Write outbox tests: transaction/event insert atomicity, retry after collector
  interruption, same ID/same bytes idempotent, same ID/different bytes conflict,
  late linked review/note events, parent-child cost dedupe, and stable tenant/run/task
  identities. Run contracts, Reckoner telemetry, platform extract/replay tests RED.
- [ ] Implement mapping from persisted domain rows to generic envelopes. Emit distinct
  calls once, root task decisions once, linked note/review/evaluation events later,
  and metric-contribution numerator/denominator as workload-owned values. Do not
  emit prompts, raw cases, labels, feature content, graph text, or API credentials.
- [ ] Export from an idempotent Postgres outbox to the existing OTLP Collector; mark
  exported only after a successful OTLP response. Retry failed exports without
  duplicate logical event IDs. Exercise collector restart with a dedicated disposable
  Compose project and test-only password.
- [ ] Replay duplicated, late, and conflicting fixtures into a disposable Touchstone
  warehouse. Verify dbt deduplication, incomplete run declarations, delayed outcome
  updates, exact currency/Decimal strings, and no v1 platform formulas. Extend
  MetricFlow/API only for supported generic dimensions/metrics.
- [ ] Add an independent expected measurement fixture and assert exact reconciliation
  for run, tenant, task, root node, call cost, outcome counts, rates, and generation.
  Use existing `touchstone verify --expected PATH --run-id ID` if it fully fits;
  extend generically only when the regression test demonstrates a missing need.
  Run Phase 2 reconciliation suite. Review and commit
  `feat: export Reckoner v1 measurements through OTLP`.

## Task 11 — Retrieval benchmark and comparison views

**Files:** create `workloads/reckoner/src/reckoner/v1/benchmark/{__init__,queries,compare,report}.py`,
`workloads/reckoner/tests/{test_v1_benchmark,test_v1_comparison}.py`,
`docs/evidence/phase-3-benchmark-method.md`; modify the v1 CLI, measurement
expectations, dashboard comparison components/tests, and report contracts as needed.

**Interfaces:** `benchmark_queries(cases: list[dict], relational, graph) -> dict`;
`compare_arms(results: list[dict], expected_ids: list[str]) -> dict`;
`comparison_eligibility(baseline: dict, current: dict) -> dict`.
Every arm pins case IDs, config/model/prompt/question/calibration/evidence versions,
retrieval window, call identities, and execution mode.

- [ ] Write RED tests for missing/extra/duplicate cases, incompatible cohort/tenant/
  currency/configuration, partial refresh generations, incomplete cost, unavailable
  graph coverage, and zero denominators. An incompatible comparison is explicitly
  ineligible and must not show a delta.
- [ ] Test an identical store query returns the same eligible case-ID set in SQL
  and Cypher for frozen cutoffs. Compare exact graph set results separately from
  pgvector top-five quality; don't equate their APIs. Test model/prompt/calibration
  changes receive separate arms and never inherit another arm's fitted evidence.
- [ ] Implement repeated read-only query measurements for cold/warm latency and
  resource use, with actual case/transaction membership and exact provenance.
  Do not trigger provider calls in the retrieval benchmark task. When decision-impact
  comparison requires new provider output, use only already persisted identical
  score results; otherwise mark unavailable pending the approved measured run.
- [ ] Produce a versioned JSON + Markdown report containing coverage, exact-set
  agreement, eligible comparables, retrieval relevance proxy, latency distributions,
  memory/disk, resource caveats, and workload result/cost only if identical persisted
  scoring exists. Label synthetic fixtures separately from measured dataset runs.
- [ ] Extend the existing dashboard with one compatible baseline/current comparison
  and arm/evidence provenance. Show incomplete/failed refresh separately; never
  average per-tenant CPST or pool synthetic workflow costs with Reckoner. Preserve
  platform independence tests. Run relevant backend, dbt, API, and web regression
  tests; reviewer gate then commit `feat: compare Reckoner v1 evidence arms`.

## Task 12 — Compose/kind, resource, and restore evidence

**Files:** create or modify `infra/compose.reckoner-v1.yaml`,
`infra/k8s/reckoner-v1/{namespace,postgres,neo4j,worker,api,web,smoke-job}.yaml`,
`infra/smoke-reckoner-v1.py`, `infra/verify_reckoner_v1_smoke.py`,
`workloads/reckoner/tests/test_v1_deployment.py`,
`docs/operations/reckoner-v1-runbook.md`, and `docs/architecture/reckoner-v1.mmd`;
modify `infra/kind.yaml`, images/CI only within current local-build patterns.

**Interfaces:** Compose and kind expose equivalent health/readiness checks and
resource profiles for preparation, review/workflow, and warehouse refresh. Runbook
commands explicitly name the project, kubeconfig, context, namespace, data paths,
and preserved-volume identifiers.

- [ ] Before start, record `df -h`, Docker usage, current cluster contexts, existing
  Compose projects/volumes, and retained image IDs read-only. Add tests that scripts
  reject ambient kubectl context, non-phase3 project names, and cleanup of any
  pre-existing resource identifier.
- [ ] Add readiness/liveness tests for API/database/Neo4j/collector dependencies,
  degraded graph availability, queue persistence, migration before traffic, and
  stopped-service states. Exercise low-resource limits for the user's 8 GB Docker
  budget; do not duplicate Phase 2's 5,888 MiB stack when starting Neo4j/workers.
- [ ] Implement separate Compose profiles for import/GDS preparation, online/API/
  reviewer workflow, and platform refresh. Give each profile a unique project and
  dedicated named volumes; never attach or reset existing phase volumes. Stop one
  heavy profile before starting the next. Set Neo4j/GDS and Postgres memory from
  actual measured headroom, not guessed service totals.
- [ ] Add kind manifests with pinned compatible image digests, resource requests/
  limits, persistent local storage, secrets mounted only at run time, smoke job,
  and isolated `KUBECONFIG`/context instructions. Compose and kind run separately.
- [ ] Write an allow-listed smoke/restore script that only deletes resources it
  created and matched by exact random sentinel/project identifiers. Test that a
  failure midway preserves all old volumes, namespaces, images, and source files.
  Verify new Postgres export/restore and graph source reimport from immutable source;
  don't claim raw ClickHouse disaster restore beyond its existing tested state.
- [ ] Measure service and combined peaks, disk growth, import/build times, shutdown
  and restart behavior; document measured limits and separate estimates. Generate
  actual LangGraph/Structurizr views and run archify drift check. Run dedicated
  Compose and kind smokes only when their runtime approvals are available, then
  independent review and commit `feat: package Reckoner v1 local runtime`.

## Task 13 — Paid run protocols, final comparison, and evidence

**Files:** create `workloads/reckoner/experiments/protocols/`,
`workloads/reckoner/src/reckoner/v1/experiment/{__init__,protocol,execute,verify}.py`,
`workloads/reckoner/tests/{test_v1_protocol,test_v1_experiment}.py`,
`docs/evidence/phase-3-reckoner-v1.md`, and screenshots only under ignored
`artifacts/phase3/screenshots/`. Modify only runbooks/contracts demonstrated to
need correction after measured verification.

**Interfaces:** `validate_protocol(protocol: dict, ledger: dict) -> dict`;
`reserve_protocol(protocol: dict, ledger) -> str`;
`execute_protocol(protocol_id: str, *, provider_clients: dict) -> dict`;
`verify_experiment(protocol: dict, run: dict, report: dict) -> dict`.
Protocols hash frozen case manifests, evidence policies, versions, token/attempt
bounds, prices, provider/ledger identities, purpose, maximum spend, and approver.

- [ ] Build offline protocol tests first. Invalid/missing key, changed case set,
  changed prices/model/attempt count, over-limit cost, wrong ledger, unresolved
  prior reservation, duplicate approval ID, or changed protocol hash blocks
  dispatch. Approval records live outside the immutable protocol body and are
  bound to its SHA. No wildcard “remaining credit” approval.
- [ ] Generate protocol drafts only after Tasks 2–12 have produced actual manifests,
  measured request-size distributions, price metadata, service receipts, and cohort
  IDs. Validate paid consent operationally before each run; generating a draft is
  not execution. See parent plan for candidate sequence/counts; revise a run's bound
  from evidence before asking, never auto-expand it after routing.
- [ ] Present one run approval with exact provider/model, frozen IDs and count,
  worst-case token/attempt cost, retry assumptions, hard USD cap, current reconciled
  provider-specific balance, nonrefundable uncertainty handling, purpose, and
  expected outputs. A provider/user's displayed credit is not sufficient approval.
  Do not start while an earlier run has unsettled or uncertain spend that could
  exceed the shared cap.
- [ ] Write RED deterministic experiment tests for incomplete expected tasks,
  replay versus live latency, missing prices, failed calls, retries, note pending,
  unknown billing, over-budget stop, and tenant/calibration/evidence mismatches.
  Any such case blocks a falsely complete/pass report. Run experiment tests without
  provider keys or network.
- [ ] For each separately approved paid protocol, reserve the full maximum before
  dispatch, execute sequentially under stop-on-limit, settle actual usage, retain
  raw outputs in ignored evidence storage, record request/response hashes, and
  reconcile actual cost. Do not rerun baseline. No provider/judge request escapes
  the protocol's case, model, attempt, or spend boundary.
- [ ] Compare final v1 with the existing v0 run on identical frozen 2019 membership.
  Report all CPST components, correctness/error/escalation rates, tenants, note
  status, costs, latencies, missing/failure counts, calibration/evidence/graph
  coverage, Touchstone refresh generation, and simulated assumptions. If quality
  gates fail, resource/cost limits stop execution, or generation is incomplete,
  report actual state and do not claim phase acceptance.
- [ ] Produce phase evidence matrix and artifact hashes; distinguish offline CI,
  disposable-store integration, paid provider runs, and real-service deployment
  checks. Verify source and baseline hashes remain unchanged; ensure secrets and
  source rows are absent from tracked files/artifacts. Complete final full test
  suite after last source fix, targeted reruns only for later fixes, whole-branch
  independent review and defect repair, resource/restore audit, and archify check.
  Commit `docs: record Reckoner v1 evidence and handoff` with reports only after
  measured facts have been verified.

## Stage C exit

The handoff states separately: code status, all offline/integration test results,
actual Compose/kind status, paid protocol approvals and spend, measurement
reconciliation, graph/comparison results, note-evaluation gate state, resource
limits, and pending owner dependencies. Never label a pending or unrun gate as
passed. Owner pushes/merges after review.

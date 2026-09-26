# 003 — Touchstone v1

**Status:** Approved by the owner on 2026-09-26; implementation plan review pending.
**Date:** 2026-09-26.
**Sources:** Approved foundation specification, completed Phase 1 evidence, and
the owner's instruction to proceed without Snowflake access for now.

## 1. Outcome and scope

Turn the saved Reckoner baseline traces into a reproducible local measurement
pipeline and a usable Next.js dashboard. Demonstrate that the same pipeline
accepts a second workflow without importing Reckoner or encoding fraud rules in
the platform. All transaction data remains explicitly simulated.

Phase 2 includes OTLP ingestion, ClickHouse raw storage, scheduled Dagster staging,
dbt models and tests, MetricFlow definitions, Elementary data-quality checks,
a FastAPI read API, the dashboard, a synthetic workflow and initial generic
evaluation-result plumbing. Docker Compose and local kind are verified separately.

DuckDB is the active local/CI warehouse. Snowflake configuration and deployment
instructions are prepared, but live Snowflake verification remains pending owner
access. A DuckDB demonstration is labelled a local preview, not a Snowflake result.
Access dependencies are tracked in `docs/operations/user-dependencies.md`.

Excluded: Jev, graph/routing changes, reviewer console, threshold editor/sweeps,
full budget and regression dashboards, authentication/onboarding, and new paid
provider runs. Existing run configuration versions are visible read-only; the
later configuration UI remains in the delivery plan.

## 2. Approach and alternatives

Use the full local processing path and preserve a warehouse boundary for later
Snowflake verification. This exercises the platform's actual aggregation and
reconciliation behavior without waiting for cloud access.

Waiting for Snowflake would prevent local delivery despite having saved evidence.
Serving the dashboard directly from ClickHouse would bypass governed warehouse
metrics. Neither alternative satisfies the intended local Phase 2 outcome as well.

## 3. Architecture and ownership

```text
Reckoner saved OTLP exports ─┐
                            ├─ OTLP Collector → ClickHouse raw traces
Synthetic workflow ─────────┘                         │
                                              scheduled Dagster
                                                     │
                                             warehouse staging
                                                     │
                                    dbt + tests + MetricFlow + Elementary
                                                     │
                                             FastAPI read API
                                                     │
                                          Next.js/TypeScript dashboard
```

The collector accepts standard OTLP traces and writes raw trace/span/event data
to ClickHouse. Dagster extracts measurement envelopes from the stored span events,
validates them against shared contracts and stages tenant-scoped warehouse rows.
Only workload-supplied generic measurements enter metric calculations.

The platform must never read Reckoner's Postgres tables, labels, raw transactions,
provider response bodies or report files to compute dashboard metrics. Reports are
an independent acceptance oracle, not a data source. Replay is performed from the
existing exports through OTLP; the platform does not import Reckoner's replay code.

FastAPI serves governed results from the configured warehouse. Credentials and
warehouse connections remain server-side. There is one Next.js app in `web/`.
Do not introduce NestJS without a concrete need.

## 4. Contracts, identity and completeness

Retain `measurement-v1` and the existing OTLP conventions. Span events carry the
canonical measurement JSON and retain tenant, workflow, run, task, node, trace,
span, configuration, cohort and code identities. Preserve the distinction between
simulated source data and fabricated measurements.

Raw deliveries remain append-only. Models deduplicate measurements by tenant,
workflow and event identity. Identical replays must not change results. Reuse of
an identity with different content is a visible conflict, not a last-write-wins
update. Invalid or unsupported envelopes are recorded as rejected with reasons;
they do not silently contribute to metrics.

Billable-call costs are counted once at their attributable provider node; parent
spans do not repeat child costs. Tenant keys participate in all joins. Incompatible
currencies, cohorts, versions and simulated/measured modes are not silently pooled.

Current events do not by themselves prove that all expected tasks were received.
Define a versioned generic run declaration contract, emitted through OTLP, with
tenant/workflow/run identity, expected task count, relevant experiment versions and
declared metric/evaluation expectations. A replay declaration is explicitly marked
as such and does not rewrite the historical provider events. It may describe a
frozen manifest but may not supply calculated baseline results to the platform.

Receipt completeness and execution success are separate. A failed task can be
fully observed; an apparently successful subset can be incompletely ingested.
Without a valid declaration, completeness is unknown. Missing prices, outcomes or
required evaluation results remain visible even when all task traces arrived.

Warehouse staging uses ingestion order/time rather than event occurrence time as
its progress boundary. Loads are repeatable and use an overlap with deduplication
so events arriving late are included. Publish refresh metadata only after the
associated transformations and tests succeed. Failed refreshes retain the last
successful result and show its age and failure state.

For contradictory outcome/evaluation revisions lacking an explicit ordering or
supersession contract, mark the affected metric incomplete. Do not choose a winner
based on arrival time. A later valid result can resolve a previously missing result.

## 5. Metric definitions

Use exact decimal money and explicit currency. All rates expose numerator and
denominator; zero denominators return unavailable. Aggregates sum components before
division rather than averaging tenant rates or CPST values.

| Metric | Definition |
|---|---|
| Online model cost | Deduplicated attributable provider-call costs; unknown pricing makes the total incomplete |
| Review and error cost | Workload-emitted generic outcome amounts, separately displayed and identified as modeled assumptions where applicable |
| CPST | `(online model cost + review cost + error cost) / correct tasks` |
| Correctness | Correct tasks / declared expected tasks |
| Completion | Tasks with completed execution / declared expected tasks; failed and missing counts shown separately |
| Latency p99 | Nearest-rank percentile across completed root-task durations; population count and failures shown |
| Evaluation pass rate | Cases passing all required checks / declared expected cases; missing/error cases cannot yield a pass |
| Workload-specific rates | Sum of generic metric-contribution numerators / sum of their denominators for one definition version |

Reckoner supplies false-positive, missed-fraud and escalation contributions; dbt
does not reconstruct these from fraud labels. Metric identifiers can appear as
data and labels without becoming hardcoded platform formulas. Drill-down exposes
costs by tenant/run/task/node and links each displayed measurement to trace/event
identities and reproducibility versions.

MetricFlow owns supported semantic definitions. Exact percentile and completeness
logic can be tested dbt models exposed through that layer; do not approximate p99
or coerce decimal cost to float to fit a library. Compatibility among dbt,
MetricFlow, DuckDB, Snowflake and Elementary must be checked against primary
documentation before versions are pinned. Any required architectural departure
must be raised explicitly rather than silently dropping a named component.

## 6. Evaluation plumbing and synthetic workflow

Accept generic suite/case/check results with pinned evaluator/judge versions and
supporting references. Preserve pass, fail, error and not-evaluated distinctions.
The baseline correctness and validity checks provide real deterministic results.

Initial case-note plumbing validates the transport and storage of evaluation
results using explicitly synthetic fixtures. It does not claim that Reckoner has
case notes, that DeepEval/Ragas judges ran, or that note quality passed. Paid judge
execution and actual note quality gates belong with the later note-producing work.

Add an independent deterministic synthetic workflow under `workloads/`, with two
tenants, several nodes, known costs, successful and failed tasks, and evaluation
events. It emits OTLP and run declarations using only shared contracts. Its costs
are fabricated and visibly labelled; it must not consume provider credentials.
Its results remain separate from measured Reckoner runs by default.

## 7. Dashboard

Use the approved dense, accessible visual direction. Provide workflow, run and
tenant selection; summary metrics; cost components and node attribution; evaluation
results; and a task/trace evidence table. Include refresh time, warehouse identity,
completeness and data/measurement simulation labels next to the relevant results.

Show the completed baseline by default, with historical pilots selectable and
their failures intact. CPST and correctness are accompanied by workload error
rates so 89.2% correctness cannot conceal 94% missed fraud. Label modeled business
cost separately from usage-priced API cost; neither is an invoice reconciliation.

The second workflow is selectable without modifying platform formulas. Missing
metrics and stale/failed refreshes have explicit states. A baseline comparison has
no improvement claim until a compatible second measured run exists. Do not fill
future reviewer, graph, agreement or threshold features with fabricated values.

## 8. Repository and operations

Use `platform/` for collector configuration, warehouse/staging code, FastAPI,
Dagster and dbt/semantic definitions; `workloads/` for the independent synthetic
emitter; `web/` for Next.js; `contracts/` for shared schemas; `infra/` for images,
Compose and kind; and `docs/` for design, runbooks and evidence.

Python follows the existing uv workspace, `src/` packages and Ruff conventions.
Frontend tooling receives a committed lockfile and ESLint/Prettier configuration.
Keep platform package names distinct from Python's standard-library `platform`.

Use bounded memory and disk retention settings appropriate to the 8 GB Docker
budget. Bring up only this phase's required services. Compose and kind run
sequentially; existing Phase 1 artifacts, backups and ledger volumes remain intact.
Do not copy secrets into images or source files. Persist collector queues and raw
events as required for verified restart/replay behavior.

Dagster supports explicit refresh and a configurable conservative schedule. It
must not start overlapping refreshes of the same warehouse. Snowflake remains
disabled without explicit configuration and access; later activation requires
credit/budget verification and 60-second auto-suspend.

## 9. Delivery sequence and acceptance

1. Verify dependency compatibility and finalize contracts and fixture expectations.
2. Implement collector/raw storage and OTLP replay/ingestion tests.
3. Implement repeatable staging, dbt metrics, semantic definitions and quality checks.
4. Implement the read API and dashboard against verified warehouse results.
5. Add the synthetic workflow and evaluation transport fixtures.
6. Reconcile the saved measured baseline, validate both deployment paths, measure
   resources, update diagrams and record the phase evidence.

Each implementation task follows TDD and independent review under the separately
reviewed plan. Verify duplicate replay, conflicting identities, late outcomes,
missing declarations/prices, failed refreshes, tenant-safe joins, decimal precision,
zero denominators, retry costs and percentile populations. UI tests exercise
filters, incomplete/stale states and evidence navigation.

The local measured baseline must reproduce 1,000 completed tasks, 892 correct,
USD 0.458940 model cost, USD 96 review cost, USD 7,225.337 error cost and CPST
USD 8.208291412556053811659192825 to the declared decimal precision. Reconcile
both tenants independently. Reproduce nearest-rank p99 1315.1606670003275 ms to
the stored duration precision, 14/900 false positives, 94/100 missed fraud and
24/1000 escalations. Verify 3,000 spans and 10,000 measurement events from the
baseline before any additional run-declaration events. Replay twice without
inflating any logical count or cost.

The synthetic workflow must reconcile against its independent deterministic
expectations and appear in the same dashboard. CI must verify platform independence
from Reckoner code and distinguish offline fixture checks from measured replay.

Exit evidence includes trace-to-metric reconciliation, tests, Compose/kind smoke,
resource measurements, architecture drift checks, screenshots of actual dashboard
results and documented limitations. Report local Phase 2 completion separately
from the pending Snowflake access/integration demonstration.

## 10. Review boundary

This document proposes the concrete Phase 2 design. After its review, write a
detailed implementation plan and present it before execution, following the
project's required methodology. No paid calls or cloud provisioning are implied.

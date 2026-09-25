# 000 — Touchstone foundation specification

**Status:** Approved by the human in this conversation on 2026-09-25. Implementation plans require their own review before execution.  
**Date:** 2026-09-25  
**Source:** `PROJECT_BRIEF.md` and the foundation discovery conversation.

This specification is the only file authorized for creation in this session. It is not an implementation plan or permission to provision resources. The decisions below describe intended behavior, not completed capabilities or measured results. Detailed contract choices proposed here remain subject to review of this document.

## 1. Draft PR/FAQ

### What is this?

Touchstone is a measurement platform for instrumented decision workflows. It will show what a change cost, which quality and operational metrics it moved, and the evidence behind the comparison. Its first workload, Reckoner, is a payments risk console that approves, declines, or escalates transactions and prepares structured notes for escalated cases.

**All transaction data is simulated.** The project uses IBM's CCTD dataset and synthetic tenants. It has not processed real customer transactions or been deployed at a company. Results will demonstrate behavior under controlled experimental conditions, not production fraud-detection effectiveness.

### Who is it for?

The intended demonstration audience is engineers and product decision-makers evaluating the cost and quality of model-assisted workflows. Reckoner supplies a concrete reviewer experience; Touchstone supplies the evidence needed to compare implementations and operating points.

### What number proves it worked?

The primary comparison is cost per successful task (CPST), expressed for Reckoner as cost per correct transaction decision. Baseline and current implementations will run on the same frozen evaluation set. Reports will expose cost components, correctness counts, false-positive rate, escalation rate, latency, and case-note quality alongside CPST. A lower CPST alone does not establish success if the quality guardrails fail.

No improvement is promised. Finding that a change is worse, a scorer is miscalibrated, or no threshold configuration satisfies the guardrails is a valid result when backed by reproducible evidence.

### Why two products in one repository?

Touchstone is the primary deliverable. Reckoner is its first workload, not a dependency of its measurement engine. OTLP is the ingestion boundary. A second, explicitly synthetic workflow must appear in the same dashboard without fraud-specific platform changes.

### What are the limitations?

The initial evaluation set has 100 fraud and 900 legitimate transactions, intentionally unlike the dataset's natural class balance. Raw results describe that enriched sample. Label-based simulated review assumes an ideal reviewer and a fixed review cost; it does not demonstrate actual human effectiveness. Graph coverage may be limited by local memory. Dataset fields do not include devices or IP addresses. Small evaluation counts make tail latency and error-rate estimates uncertain.

### How are business costs represented?

Review costs $4 per escalation. A missed fraud costs the transaction amount. A false decline costs 30% of the legitimate transaction amount. Customer-trust effects are excluded because no defensible monetary value has been supplied. These are versioned experimental assumptions, not empirically established business costs.

### What about tenant privacy?

The demonstration deliberately permits cross-tenant graph analysis through shared entities. Tenant-scoped operational records still carry tenant identifiers. This demonstrates a product tradeoff, not a secure multi-tenant isolation system; authentication, RBAC, and enterprise privacy controls are outside this project's scope.

## 2. Scope and priorities

### Included

- Touchstone: OTLP ingestion, raw events in ClickHouse, scheduled warehouse loading, tested dbt metrics, MetricFlow, quality checks, evaluations, comparisons, budgets, and a metrics dashboard.
- Reckoner v0: one Anthropic model call per evaluated transaction, returning an approve, decline, or escalate decision; no routing, graph, retrieval, or application caching.
- Reckoner v1: Jev scoring, LangGraph orchestration, graph and retrieval evidence, amount-aware routing, structured Anthropic case notes for escalations, and reviewer actions.
- One Next.js application containing the reviewer console, metrics dashboard, and threshold administration.
- Two synthetic payment-business tenants and a separate synthetic second workflow.
- A measured Neo4j/GDS versus Postgres/pgvector comparison, calibration checks, and a constrained threshold sweep.
- Docker Compose and a single-node local kind deployment path from the foundation phase.
- Architecture, sequence, lineage, and workflow diagrams maintained through delivery.

### Excluded

SSO, RBAC, an enterprise audit-log product, billing, customer onboarding, data residency, warehouse-native deployment, per-tenant mapping DSLs, self-hosted LLMs, GPU serving, and the explicitly excluded tools in the brief. Reviewer action records and measurement provenance are required domain records, despite the exclusion of an enterprise audit-log feature.

NestJS is available only if a concrete later service requirement justifies it. No NestJS service is part of the foundation design. Protect Touchstone if time or resources force a scope discussion.

## 3. Agreed environment and engineering conventions

| Area | Decision |
|---|---|
| Repository | Existing `touchstone` directory; one repository |
| Backend | Python, FastAPI |
| Workload orchestration | LangGraph |
| Frontend | Next.js and TypeScript |
| Python tooling | uv, `pyproject.toml`, committed lockfile, `src/` package layouts |
| Conventions | Ruff; ESLint and Prettier; Conventional Commits |
| Scoring | Jev; wait for access, with no stub implementation |
| LLM | Anthropic through LiteLLM |
| Local resources | 8 GB RAM, 4 GB swap; use the current approximately 80 GB free disk, confirmed during planning |
| Local deployment | Compose and single-node kind, used separately |
| Warehouse | DuckDB for local/CI; Snowflake for the demonstrated dashboard once access is available |
| Budget | Provisional $5–10 monthly cap per paid service; $10 total initial LLM baseline/evaluation budget, including judges |
| Agent tooling | Use existing Superpowers installations; Codex skills are available, Claude installation is unverified |

No paid resources are provisioned implicitly. A budget cap is a limit, not a guarantee that the requested run fits. Measure a small paid pilot within the cap before launching all 1,000 cases. If the full run cannot fit, stop and report the tradeoff; do not silently shrink the cohort or change providers.

The entire stack fitting in 8 GB is unverified. Only the services required by the current phase should run. Compose and kind must not duplicate the stack simultaneously. Disk-backed batching is permitted; swap is not treated as additional reliable working memory. Resource measurements determine active graph size, not the brief's tentative 2–5 million-row estimate.

## 4. Data, sampling, and experimental design

### 4.1 Available source files

Read-only inspection found these files in `archive/`:

- `credit_card_transactions-ibm_v2.csv` — main transaction dataset, approximately 2.2 GB.
- `sd254_cards.csv` — card attributes.
- `sd254_users.csv` — user attributes.
- `User0_credit_card_transactions.csv` — an overlapping-looking subset; do not ingest alongside the main file without proving it adds distinct records.

Inspected transaction headers include user/card references, date/time, amount, payment channel, merchant identifier/location/category, errors, and the fraud label. No device or IP columns are present. Row counts, join semantics, timestamp timezone, currency assumptions, duplicate behavior, and value distributions have not yet been validated.

Preserve source files unchanged. Do not place the dataset in git. Raw card numbers, CVVs, addresses, and unnecessary personal attributes must not enter prompts or telemetry; the measured use case needs stable entity references, not those fields.

### 4.2 Retained data and tenant assignment

Retain every fraud row in the source corpus and complete transaction histories for selected users/cards, including those needed to retain the fraud rows. Do not row-sample the histories used for graph construction. Add legitimate histories by entity as resources permit. Process retained data in batches when needed.

The two tenants represent payment businesses serving multiple merchants. Target approximately 70% of transactions in a larger, lower-risk tenant and 30% in a smaller, higher-risk tenant. Assign each complete user/card history to one tenant; exact ratios are secondary to preserving histories. Shared merchant identities connect the tenants.

Freeze and version the assignment method and seed. Prefer constructing risk-profile differences from pre-holdout activity; never feed holdout labels into runtime features. Report achieved sizes and fraud rates rather than asserting the target profiles were achieved.

Tenant splitting and entity sampling are separate operations. Each requires a manifest recording selected entities, source checksum, rules, seed, counts, and class composition. Retaining all source fraud does not mean all fraud appears in a 1,000-case evaluation run.

### 4.3 Evaluation cohort and temporal boundaries

The initial frozen cohort contains exactly 1,000 unique transactions: 100 fraud and 900 legitimate. Both v0 and v1 use the same cases and tenant assignment. Freeze a time-based holdout and cohort manifest after validating the source. Keep calibration/development data separate from the reported comparison.

For each evaluated transaction, scoring, graph features, and retrieval may use only evidence available before its timestamp. Historical labels may be used only after a defined resolution time; absent a defensible availability time, they are excluded from historical runtime features. Evaluation labels are stored separately and accessible to the evaluator and simulated reviewer, not the scorer or case-note prompt.

Retain full selected histories on disk while constructing time-correct graph views. A graph snapshot must not reveal future edges, future fraud flags, or future resolved cases. The enriched selection itself may use labels, but those labels cannot accompany runtime inputs.

If a temporally valid 100/900 cohort cannot be formed, report the issue and revisit the cohort definition before claiming a baseline. Failures and missing predictions are reported against the fixed cohort, never silently dropped.

### 4.4 Graph coverage and sampling claims

Use observed users/accounts, cards, merchants, and transactions in the measured graph. Synthetic device/IP edges may appear only in a separately labelled demonstration with separate results.

An active graph subset is permitted if its entity/history coverage is explicit. Baseline/current graph comparisons must use declared, reproducible coverage and time windows. If the required complete graph cannot fit, stop and revisit graph scope rather than silently truncating it. Ring detection and centrality must earn their place through measured utility; detected communities alone are not proof of fraud rings.

Report sample metrics as sample metrics. Never apply a single sampling ratio to every metric. Population-adjusted estimates require a documented estimand and justified inclusion probabilities/weights, including the entity-selection stage. If these cannot be established, the estimate is unavailable. Class enrichment correction alone does not correct entity-selection bias.

## 5. Architecture and ownership

### 5.1 Runtime flow

1. The Reckoner CCTD adapter validates and maps source rows to the canonical transaction contract, retaining source provenance and separating oracle labels.
2. FastAPI exposes operational APIs backed by Postgres. Reckoner workers execute v0 or the LangGraph v1 pipeline.
3. V1 obtains Jev scores, builds graph/retrieval evidence as required, makes versioned routing decisions, and generates notes only for escalations.
4. Workload instrumentation emits traces and generic evaluation/outcome measurements through OTLP to the Touchstone collector, which exports to ClickHouse.
5. Dagster periodically copies raw events from ClickHouse into Snowflake staging and runs dbt, tests, Elementary checks, and metric refreshes there. Local/CI runs stage the same event format into DuckDB.
6. MetricFlow supplies governed metric definitions. The Next.js dashboard accesses warehouse-backed results through a server-side API; warehouse credentials never reach the browser.
7. The reviewer console and settings surfaces call operational APIs. Human or simulated reviews are persisted in Postgres and instrumented through OTLP.

The dashboard's demonstrated warehouse is Snowflake. DuckDB supports local development and CI and must be identified as such when used for a local preview. Cross-database staging is explicit; dbt is not assumed to directly write to a second warehouse from its source connection.

### 5.2 Boundaries

- Touchstone ingests workload data **only through OTLP**. It never imports Reckoner code or reads Reckoner's operational tables to calculate metrics.
- dbt models use generic workflow dimensions including `workflow_id`, `node_name`, and `tenant_id`. Fraud labels, routing business rules, and fraud-loss formulas belong to Reckoner.
- Reckoner emits generic correctness, review-cost, and error-cost outcomes. Touchstone performs tested, workflow-independent aggregation.
- A synthetic second workflow emits real OTLP traces with explicitly synthetic costs/outcomes and appears in the dashboard. Its artificial results are never pooled into Reckoner's measured results by default.
- Every persisted application/event row and graph node has `tenant_id`; tenant-specific joins include it. Experiment/configuration metadata shared between tenants is represented with tenant-scoped records sharing a version identifier.
- Shared graph entities are represented as tenant-scoped occurrences with a common canonical entity identifier and explicit cross-tenant identity relationships. This preserves tenant attribution while permitting the agreed shared-graph analysis.

### 5.3 Store responsibilities

| Store | Responsibility |
|---|---|
| Postgres + pgvector | Transactions, decisions, cases, immutable configurations, review actions; resolved-case retrieval and relational/vector baseline |
| Neo4j + GDS | Time-correct entity relationships, neighbourhood evidence, community detection, centrality |
| ClickHouse | Append-only raw telemetry; Touchstone's source of truth |
| Snowflake | Staged events and governed modeled marts for demonstrated dashboard queries |
| DuckDB | Local/CI target for equivalent logical metric definitions |

Warehouse reloads must be idempotent using stable event identities and watermarks. Raw delivery duplicates may be retained; analytical models deduplicate before counting. Late evaluation/review events update modeled outcomes without overwriting raw history. Do not sum parent and child costs twice.

### 5.4 Graph and failure behavior

Hand-write Cypher. Evaluate GDS community detection and PageRank/centrality on the actual retained graph. Compare Postgres/pgvector and Neo4j on declared tasks, identical temporal evidence, retrieval quality, runtime, and decision impact; similarity search and community detection are not interchangeable capabilities.

Provider failures must not drop a transaction. A circuit breaker routes unavailable scoring to a conservative rules-based escalation, explicitly marked as degraded. This is a runtime fallback after a real integration exists, not a Jev development stub. Failed note generation retains the escalation with a visible missing-note status. Failed or unpriced calls retain their status and known cost; missing cost is not zero.

## 6. Canonical contracts

Contracts are plain, versioned files in `contracts/`, consumable by Python and TypeScript. The following is the foundation contract specification; actual schemas and validation code are later work.

### 6.1 Canonical transaction event

| Field | Type and meaning |
|---|---|
| `schema_version` | Required version string |
| `event_id` | Required stable identifier for idempotent ingestion |
| `tenant_id` | Required synthetic payment-business identifier |
| `transaction_id` | Required stable transaction identifier |
| `occurred_at` | Required normalized timestamp with a documented source timezone assumption |
| `account_id`, `card_id` | Required stable, tenant-scoped entity identifiers |
| `merchant_id` | Required stable merchant identifier; cross-tenant identity is represented separately |
| `amount_minor` | Required integer amount in minor currency units; never binary floating-point money |
| `currency` | Required currency code; an assumed source currency must be explicitly recorded in provenance |
| `payment_channel` | Normalized enum: `chip`, `swipe`, `online`, `other`, `unknown` |
| `merchant_category_code` | Nullable string |
| `merchant_location` | Nullable structured city, region, postal code, country; preserve unknowns |
| `processing_errors` | Array of normalized source error values, possibly empty |
| `device_id`, `ip_address` | Nullable; absent for the measured CCTD workload |
| `provenance` | Dataset/version, source-file checksum, source-record reference, adapter version, `simulated=true`, normalization assumptions |

No CCTD column names appear in Cypher, prompts, or business logic. They are confined to adapter mapping and source documentation. If the source lacks a transaction identifier, derive one deterministically from the source manifest and record position; do not merge distinct records merely because their fields match.

The initial routing formulas apply to positive-value purchases. Zero/negative amounts and unparseable timestamps or identifiers are retained with explicit unsupported/validation status and excluded from the eligible cohort with counts reported. Currency and timezone normalization must be validated and documented before freezing the cohort; the adapter must not invent certainty about either.

### 6.2 Evaluation oracle

Oracle records contain `tenant_id`, `transaction_id`, `label` (`legitimate` or `fraud`), source provenance, and oracle version. They are separate from canonical runtime transaction payloads. The evaluator joins them after the decision. Simulated reviews record `reviewer_type=simulated`, the oracle version, and the label-derived verdict.

### 6.3 Decision and case-note records

Every decision records tenant/transaction/workflow/run identifiers, decision ID, timestamps, outcome, scorer/model version, prompt version where applicable, threshold-config ID, graph snapshot and cohort versions, actual score when available, effective thresholds, evidence references, and degraded/failure status. An immutable decision is not overwritten by a later review.

Allowed outcomes are exactly `auto-approve`, `auto-decline`, and `escalate`. Jev supplies the v1 probability; a baseline model judgment must not be represented as a calibrated Jev probability.

An escalated case note has a fixed schema:

- `verdict_recommendation`: approve or decline.
- `confidence`: sourced from Jev, with its meaning and scorer version recorded.
- `risk_factors`: up to three ranked, evidence-linked factors sourced from feature attribution; explicitly identify insufficient evidence instead of inventing three. Scorer attribution availability is an integration check, not permission to have the LLM invent it.
- `entity_neighbourhood_summary`: grounded statements with graph evidence references and time window.
- `comparable_past_cases`: references to resolved, temporally eligible cases, or an explicit empty result.
- `what_would_change_verdict`: concrete additional evidence or reviewer action.

Notes must preserve provenance for evaluation. Confidence comes from the scorer; the language model does not manufacture a separate calibrated probability. A label-as-oracle simulated reviewer does not prove that a human could decide from the note alone.

### 6.4 OTLP measurement envelope

Use OpenTelemetry trace/resource attributes and span events, pinning the chosen GenAI semantic-convention version during implementation. Custom fields use a documented project namespace; none are presented as official OTel attributes.

Required logical fields:

- Identity: `schema_version`, `event_id`, `tenant_id`, `workflow_id`, `workflow_version`, `run_id`, `task_id`, `trace_id`, `span_id`, `node_name`, event kind, event timestamp.
- Reproducibility: experiment/cohort/config versions, code revision, prompt/model/scorer versions where applicable, dataset and graph versions, synthetic/real-call indicator.
- Execution: start/end times, status, attempt number, parent references, provider/model, input/output/cached token usage where available.
- Cost: currency, actual/estimated/unavailable status, price-table version, provider-call cost, and unique call identity. Cost is attributed once at the billable call; node/run totals are derived.
- Outcome: nullable `correct`, `review_cost`, `error_cost`, outcome version, evaluator version, and observed/pending/failed status. Reckoner supplies business semantics; the platform does not recalculate them.
- Evaluations: suite/case/metric identifiers, score, threshold, pass/fail/error status, judge model/prompt version, and supporting references.
- Domain metric contributions: workload-defined `metric_id`, numerator, denominator, unit, definition version, and optional cost-component identifier. Reckoner emits the counts for false-positive and escalation rates and the fraud/false-positive cost breakdown; generic dbt models aggregate contributions without recognizing those domain identifiers or averaging per-run percentages.
- Review: recommendation and review references, reviewer type, agreement result, and review timestamp. These are generic workflow measurements, not raw fraud labels.

Emit outcome and review events as linked later events when the original span has ended. Preserve unique logical task identity across retries. Duplicate delivery must not inflate attempts, decisions, or costs. Raw prompts, card details, and full source rows are not required telemetry.

## 7. Routing and configuration

Defaults are immutable within a configuration version:

| Parameter | Default |
|---|---:|
| `review_cost` | $4.00 |
| `margin_rate` | 0.30 |
| `t_low_floor` | 0.005 |
| `t_low_ceiling` | 0.05 |
| `t_high` | 0.90 |
| `amount_aware` | true |

For a positive transaction amount expressed in the same currency as review cost:

`t_low = clamp(review_cost / amount, t_low_floor, t_low_ceiling)`.

Flat mode uses a fixed `t_low = 0.05` in v0 of the configuration contract, independently of later edits to the amount-aware ceiling. Below `t_low`, approve; above `t_high`, decline; equality at either boundary escalates. Validate probability and threshold ranges and require `0 <= floor <= ceiling < high <= 1`; flat mode additionally requires `t_high > 0.05`.

Every edit creates a new configuration version. Historical decisions keep their original version. Admin preview is a labelled estimate/replay, not an executed experiment. The flat versus amount-aware comparison holds cohort, scores, evidence, model versions, and other configuration values constant.

## 8. Metrics and evaluation definitions

All displayed metrics include tenant/workflow, run/cohort, implementation/configuration versions, counts, data completeness, and refresh time. Undefined denominators display unavailable, never zero. Report absolute and relative deltas only when their denominators are meaningful.

### 8.1 Correctness and CPST

For the simulation:

| Decision | Legitimate label | Fraud label |
|---|---|---|
| Auto-approve | Correct | Incorrect; loss = amount |
| Auto-decline | Incorrect; loss = amount × margin rate | Correct |
| Escalate | Correct but incurs review cost | Correct but incurs review cost |

`CPST = (model_cost + review_cost × escalation_count + fraud_loss + false_positive_loss) / correct_decision_count`.

`model_cost` includes attributable online scoring, generation, retrieval-embedding, and retry charges. Offline judge/evaluation cost and infrastructure spend are reported separately; judges still consume the agreed initial $10 LLM budget. Incomplete pricing makes CPST incomplete, not artificially cheaper. If no decisions are correct, CPST is undefined and the run cannot be a winning configuration.

Escalation counts as correct under the agreed ideal-review assumption, regardless of whether a human has clicked yet. This is not a claim that every real review succeeds. Human agreement and observed review outcomes are separate measures.

### 8.2 Operational and business metrics

| Metric | Definition |
|---|---|
| False-positive rate | Legitimate transactions auto-declined / all legitimate transactions in the fixed cohort |
| Missed-fraud rate | Fraud transactions auto-approved / all fraud transactions in the fixed cohort |
| Escalation rate | Escalated transactions / all transactions in the fixed cohort |
| Correct-decision rate | Correct decisions / all transactions in the fixed cohort; pending/failed cases do not become correct |
| Completion coverage | Transactions with terminal decisions / fixed cohort size |
| Online latency | First task start to terminal decision, including retries; also report time until note readiness for escalations and individual attempt latencies |
| p99 latency | Empirical nearest-rank 99th percentile of the declared completed-task latency population, with count and failures alongside it |
| Cost by node/run/task/tenant | Deduplicated attributable billable-call costs grouped at that grain |
| Cost to serve | Online model cost plus simulated review cost; infrastructure costs displayed separately when attributable |
| Agreement rate | Reviewed cases where approve/decline action matches the pre-review recommendation / reviewed cases with both values, split by human versus simulated reviewer |
| Task lead time | Intake to final resolution; report automatic, human-reviewed, and simulated-reviewed populations separately |
| Adoption | Distinct human reviewer identities active and number of human review actions per period; simulated activity excluded |

The dashboard also shows review, error, online-model, and offline-evaluation cost components separately. No tenant profitability or revenue claim is made without actual defined revenue inputs.

DORA's four-key vocabulary applies only to observed deployment activity: deployment frequency, change lead time, change failure rate, and recovery time. It is not inferred from transaction processing. Until qualifying deployment/incident events exist, those metrics are unavailable. SPACE contributes only the explicitly limited adoption dimension.

### 8.3 Evaluation suites

- Deterministic decision tests: threshold boundaries, amount-aware calculations, routing, asymmetric costs, configuration versions, and failure handling.
- Calibration: Jev score buckets versus observed fraud rate on a separate, temporally valid calibration population; include bucket counts and weighting assumptions. Do not assess natural-prevalence calibration directly from the unadjusted 100/900 cohort.
- Case-note schema validity: **100%** of expected notes must satisfy the contract.
- Note-only verdict evaluation: a pinned DeepEval judge receives the note without raw transaction evidence, produces an approve/decline verdict, and the evaluator compares it with the oracle; **at least 90% agreement** is the provisional gate.
- Faithfulness: Ragas checks note claims against the exact retrieved evidence supplied to generation; **mean score at least 0.90** is the provisional gate. Report per-case failures and distribution, not just the mean.
- Missing notes and judge errors are visible failures/incomplete evaluations, never quietly removed to achieve a passing score. Zero eligible cases means not evaluated, not passed.
- Human agreement is reported independently and never replaced by judge or oracle agreement.

Record judge model, prompt, library version, generation settings, and case IDs. DeepEval/Ragas API details and model compatibility require validation after approval; tool failures do not justify substituting an unlabelled metric.

An eval suite's pass rate is passing cases / all expected cases, with failed execution counted separately and unable to yield an overall pass. A case passes only when all its required checks pass. Coverage is mapped from an explicit registry of workflow nodes/paths to suites and observed evaluated executions. A lack of executions is not proof of coverage.

CI runs deterministic/unit/integration/dbt checks and the declared case-note quality gates when relevant. Paid judge execution must use an approved fixture size and remaining budget; unavailable credentials or exhausted budget produce an incomplete gate, not a pass. Cached past judge results are labelled and cannot be represented as fresh validation of changed notes.

### 8.4 Threshold sweep and comparisons

Evaluate a declared grid of low/high thresholds and the amount-aware switch against frozen scores and labels. Record all grid values and effective thresholds. Mark configurations rejected when **false-positive rate exceeds 1%** or **escalation rate exceeds 5%** on the enriched cohort. Annotate the chosen operating point and show when no point qualifies.

For the agreed 900 legitimate cases and 1,000 total cases, these constraints allow at most nine false declines and 50 escalations. Report uncertainty; these sample limits do not establish population compliance.

Replaying routing cannot establish actual generation cost for newly escalated cases. Sweep costs must either come from measured per-case executions or be labelled estimates. Confirm the selected point with a measured run before claiming a CPST improvement. Avoid repeatedly tuning on the same cohort and calling the resulting number an independent test result; a final confirmation cohort is required for that stronger claim.

Baseline/current views preserve identical cases, oracle, cost assumptions, currency, and time boundaries. Show effects by tenant as well as aggregate. A fixed dataset does not imply deterministic provider output; retain actual responses and run identifiers.

### 8.5 Budgets

Track online and offline provider usage in one initial LLM budget ledger. Reserve estimated maximum call cost before dispatch, reconcile actual cost afterward, and stop new calls when the remaining budget cannot cover them. Display an early warning at 80% usage and a hard limit at the agreed cap; unknown pricing blocks budgeted dispatch rather than assuming free usage. Initial alerts appear in the application/logs; external messages require separate authorization.

Use Snowflake's smallest suitable warehouse with 60-second auto-suspend and scheduled transformations. Credits and trial availability must be confirmed before use. No trial availability or post-trial affordability is assumed.

## 9. Repository layout

```text
touchstone/
├── platform/           # OTel collector, ClickHouse, dbt, Dagster, generic measurement
├── workloads/
│   └── reckoner/       # adapter, FastAPI, pipeline, graph, decisions, notes, domain evals
├── web/                # one Next.js app: console, dashboard, admin
├── contracts/          # canonical transaction/measurement schemas and OTel conventions
├── infra/              # Docker Compose and local kind Kubernetes manifests
└── docs/               # specifications, PR/FAQ, Structurizr, ADRs, Mermaid
```

The synthetic second workflow belongs under a new `workloads/` folder. It depends on measurement contracts, not Reckoner. Python packages use `src/` internally without changing these top-level boundaries. `archive/` is local source data, not a new application boundary or a tracked artifact.

`AGENTS.md` and `CLAUDE.md` retain equivalent plain-file guidance. Agent-specific installed state is not the repository's source of truth. This session does not modify either file or install tooling.

## 10. User-facing behavior

The reviewer console has a dense keyboard-first queue, structured notes, an inline graph neighbourhood, and approve/decline actions. Support `j`/`k` navigation and `a`/`d` review actions with accessible focus handling. Show missing evidence, degraded execution, and simulated review explicitly. Do not create a chat interface or decorative AI branding.

The dashboard exposes cost, quality, latency, baseline comparisons, tenant attribution, agreement, coverage, budgets, and the threshold sweep. Its refresh timestamp distinguishes seconds-fresh raw events from scheduled warehouse metrics. Evidence links connect a metric to its run/configuration/cohort and trace references.

Admin exposes the six threshold settings, a labelled preview, and immutable version history. Saving a version never rewrites previous decisions. UI references are Linear, Stripe Dashboard, and Datadog: dense, accessible, responsive, and task-focused.

## 11. Phased delivery and acceptance gates

These phases describe outcomes and dependencies. They do not replace the separately reviewed implementation plans required after specification approval.

### Phase 0 — Approved foundation and PR/FAQ

Review this specification, including the opening PR/FAQ. After approval, write a detailed plan for the first unit of work. Validate dataset semantics, source counts, joins, financial units, timestamp assumptions, host resources, and installed tooling. Establish cohort/config/contract versioning, initial diagrams, test tooling, Compose, and single-node kind support in an isolated worktree.

**Exit evidence:** approved design and plan; validated data assumptions; reproducible cohort construction definition; resource report; both deployment paths smoke-tested for the services introduced. Jev remains blocked until access is supplied; no stub or substitute scorer is created.

### Phase 1 — Reckoner v0 baseline

Implement the adapter, canonical contracts, operational persistence, and naive Anthropic baseline. Capture OTLP traces from the first measured calls and provide a local trace export for later ingestion so Phase 2 can reproduce the actual baseline. Basic spend tracking and a hard dispatch cap are required before any paid pilot; Phase 4 adds the full dashboard and alert experience. Validate cost with a small pilot before completing the frozen 1,000-case run. The single-call baseline is the explicitly approved exception to the v1 case-note-only LLM rule.

**Exit evidence:** traceable decisions and labels for the declared cohort, measured costs and latency, correctness/CPST components, versioned prompts/configuration, failure counts, and reproducible baseline artifacts. If cost or access blocks completion, label the run incomplete.

### Phase 2 — Touchstone v1

Stand up collector → ClickHouse → scheduled Dagster staging → dbt/MetricFlow → dashboard. Use DuckDB for local/CI tests and Snowflake when access/credits are available. Add the synthetic second workflow in this phase so independence is demonstrated early. Include initial case-note eval plumbing and per-tenant cost attribution.

**Exit evidence:** tested generic metric definitions, deduplication and late-event tests, trace-to-metric reconciliation, baseline visible in dashboard, second workflow visible without platform fraud logic, and explicit warehouse refresh/completeness state. Snowflake-backed demonstration remains an access gate, not an assumed accomplishment.

### Phase 3 — Reckoner v1

After Jev access, integrate scoring and check calibration before trusting thresholds. Add LangGraph routing, Neo4j/GDS, Postgres/pgvector comparison, temporally valid retrieval, structured Anthropic notes, reviewer console, simulated reviewer, and configuration history.

**Exit evidence:** meaningful graph/relational comparison, time-leakage tests, scorer calibration report, case-note eval gates, observed baseline/current metrics on the same cohort, and a resource report. No requirement to fabricate a graph benefit or CPST improvement if the experiment finds none.

### Phase 4 — Touchstone v2

Complete constrained threshold sweeps, measured confirmation of the selected operating point, budget enforcement/early alerts, eval coverage and regression views, human/simulated agreement separation, and tenant cost-to-serve views.

**Exit evidence:** reproducible sweep with rejected regions, clear measured-versus-estimated costs, chosen-point evidence or an explicit no-feasible-point result, tested budget controls, traceable regression cases, and documented limitations.

### Workflow for every approved unit

`brainstorm → spec → plan → worktree → TDD → subagent execution → code review → finish-branch`.

Each feature uses an isolated worktree/branch. Tests precede implementation. Each implementation task uses a fresh subagent with clean context; self-review and code review precede declaring the branch complete. Keep the process compatible with both Codex and Claude Code through plain repository artifacts.

Use Structurizr DSL for C4, Mermaid for sequence/state/flow, generated dbt docs for lineage, and LangGraph's generated diagram for the pipeline. Run an archify drift check at each phase end after code exists. Do not introduce decorative patterns; each abstraction must serve an actual requirement.

## 12. Resolved conflicts and remaining verification gates

### Explicitly resolved in discovery

- PR/FAQ appears first in this specification to respect its precedence over architecture while creating only one file.
- V0 may use an LLM for all 1,000 decisions; v1 restricts it to escalated case notes.
- Preserve fraud rows and selected full histories; use batching and declare graph coverage, stopping for scope review if a complete required graph cannot fit.
- Device/IP fabrication is excluded from measured results.
- Reckoner owns fraud-specific calculations; Touchstone ingests generic outcomes over OTLP.
- Dagster performs ClickHouse-to-warehouse transfer before target-local dbt transformations.
- Flat approval threshold defaults to 0.05; false-positive cost excludes unquantified customer trust.
- Local/CI uses DuckDB; demonstrated dashboard uses Snowflake once available.
- Dataset labels power a distinct simulated reviewer; they do not substitute for human agreement evidence.

### Verification gates, not assumptions of availability

Jev credentials/rate limits; Anthropic credentials and measured affordability; Snowflake access/credits; exact dataset counts and joins; currency/timezone and invalid-amount treatment; graph usefulness and resource fit; actual installed Claude skills; compatibility of the selected OTel, MetricFlow, dbt, Elementary, DeepEval, and Ragas versions. These checks occur only after approval under the relevant implementation plan. Failures must be reported and cannot silently relax this specification.

## 13. Approval boundary

The human explicitly approved this specification on 2026-09-25 and authorized proceeding to planning. Discovery answers alone did not authorize implementation. Review of the written implementation plan is the next gate; implementation, installation, repository scaffolding, commits, worktree creation, paid calls, and deployment remain subject to that gate and the required workflow.

# Reckoner v1 — Evidence and Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce reproducible, time-valid evidence, a budgeted Jev adapter, and calibration reports without changing the v0 baseline.

**Architecture:** Strict v1 documents and additive Postgres tables support streamed history preparation and interchangeable SQL/graph evidence. A narrow provider adapter records every attempt; separate evaluator-only tooling joins labels for calibration.

**Tech Stack:** Existing Python 3.12/uv/Postgres, pgvector, Neo4j/GDS, synchronous HTTP, NumPy/SciPy for explicit calibration calculations.

**Spec:** [Approved Phase 3 specification](../../spec/004-reckoner-v1.md).

## Global Constraints

All [parent-plan constraints, inventory, and execution gates](2026-09-30-reckoner-v1.md)
apply. This stage is Tasks 1–5 on the shared feature branch. No paid requests are
authorized by this plan. All test HTTP transports are explicitly fabricated.

## Review Focus

Parent risks 1, 2, and 5 are pinned by Tasks 2–4; Tasks 1 and 5 prevent incompatible
record/model versions and validation leakage. No developer may quietly choose a
different cohort, history subset, temporal cutoff, or calibration population.

## Task 1 — Strict v1 records and additive storage

**Files:** create `workloads/reckoner/src/reckoner/v1/__init__.py`,
`workloads/reckoner/src/reckoner/v1/contracts.py`,
`workloads/reckoner/src/reckoner/v1/storage/{__init__,repository}.py`,
`workloads/reckoner/src/reckoner/storage/migrations/005_v1_records.sql`,
`contracts/schemas/reckoner-{run-config,evidence,score,decision,case-note,review,resolution,experiment}-v1.schema.json`,
`contracts/tests/test_reckoner_v1.py`,
`workloads/reckoner/tests/test_v1_storage.py`,
`workloads/reckoner/tests/v1_fixtures.py`, and
`docs/operations/reckoner-v1-compatibility.md`; modify root `pyproject.toml`,
`workloads/reckoner/pyproject.toml`, `uv.lock`, `.github/workflows/ci.yaml` only for
the dependencies/markers actually needed by this stage.

**Interfaces:** `validate_v1(kind: str, document: dict) -> dict` returns a validated,
detached document; supported kinds match the new schema stems. `V1Repository(dsn)`
is a context manager with `register_config(document) -> str`,
`create_run(manifest: dict, config_id: str) -> dict`,
`task(tenant_id: str, run_id: str, task_id: str) -> dict`,
`persist_evidence(document: dict) -> str`, and
`persist_decision(document: dict) -> dict`. It never exposes its owner connection
to a provider. Later tasks add focused repository modules instead of a giant class.

- [ ] Verify primary dependency metadata against Python 3.12 and the existing uv
  lock. Add only immediate dependencies; record planned LangGraph/DeepEval/Ragas
  constraints for their owning tasks. Pin compatible HTTP client and numeric
  libraries, verify an installed Reckoner wheel includes the new schemas, and add
  `neo4j_integration` marker with correct offline/CI exclusions. No provider keys.
- [ ] Write schema tests for missing tenant, unknown keys, nonfinite probabilities,
  invalid sums, incompatible config hashes, and invalid model/price combinations.
  Pin these assertions with fixture builders in `v1_fixtures.py`:
  `validate_v1('score', score_fixture(fraud='0.2', legitimate='0.8'))['raw_probability'] == '0.2'`;
  a fixture sum `0.2 + 0.7` raises `ValueError`; a degraded decision with
  `call_id=None, raw_probability=None` validates; a successful decision with those
  nulls does not. Note fields preserve the spec's six-field structure and provenance.
- [ ] Run `uv run --all-packages pytest contracts/tests/test_reckoner_v1.py -q`;
  record RED for absent behavior, not an unrelated dependency/import failure.
- [ ] Implement strict schemas, identity validation, and `005_v1_records.sql`.
  Add tenant-scoped `v1_configs`, `v1_runs`, `v1_tasks`, `v1_evidence`,
  `v1_decisions`, `v1_cases`, `v1_notes`, `v1_reviews`, and `v1_outbox` tables under
  `reckoner`. Keep v1 run purposes independent of baseline constraints. Store
  immutable documents plus indexed identities/status/version fields. Reuse canonical
  transaction documents; do not edit applied migrations 001–004 or baseline schemas.
- [ ] Add integration tests: applying migrations twice is harmless; changing applied
  bytes fails; a seeded baseline remains readable; foreign-tenant references fail;
  duplicate identical decision is idempotent, conflicting contents fail; runner/API
  cannot read `oracle.oracle_labels`. Initially API views are read-only. Run
  `uv run --all-packages pytest workloads/reckoner/tests/test_v1_storage.py -m integration -q`
  against a disposable Postgres, first RED then GREEN after repository implementation.
- [ ] Run focused schema/storage tests and Ruff, including original storage/API
  regressions. Self-review, independent review, then commit
  `feat: define versioned Reckoner v1 records` using only this task's files.

## Task 2 — Temporal history preparation and frozen experiment manifests

**Files:** create `workloads/reckoner/src/reckoner/v1/data/{__init__,prepare,history,sampling}.py`,
`workloads/reckoner/src/reckoner/v1/cli.py`,
`workloads/reckoner/src/reckoner/storage/migrations/006_v1_history.sql`,
`workloads/reckoner/tests/{test_v1_history,test_v1_sampling,test_v1_preparation}.py`,
`docs/data/phase-3-experiments.md`; modify `workloads/reckoner/src/reckoner/cli.py`
to register the v1 command group. Original source adapter stays the sole CCTD mapper.

**Interfaces:** `prepare_v1(source: Path, baseline_bundle: Path, output: Path) -> dict`;
`historical_resolution(transaction: dict, label: str, policy_id: str) -> dict`;
`eligible_before(occurred_at: str, available_at: str, query_at: str) -> bool`;
`select_sample(records: Iterable[dict], *, year: int, seed: int, excluded_ids: set[str]) -> dict`.
CLI: `reckoner v1 prepare --source PATH --baseline-bundle PATH --output NEW_PATH`;
`reckoner v1 import --bundle PATH --env-file PATH`. Output contains separate
runtime/oracle files, source-backed history index, and immutable manifests; only
the privileged import/preparation role can access oracle/resolution source tables.

- [ ] Write temporal assertions: a transaction at `2017-12-25T00:00:00Z` resolves
  exactly at `2018-01-01T00:00:00Z` and is excluded from 2018 availability; one
  second earlier qualifies. Equal-time transactions are excluded. A historical
  label at `t+7 days` is unavailable at equality and available one second later.
  Test shuffled source order, duplicate-looking distinct records, source hash
  changes, runtime/oracle field separation, and rejection of reused output paths.
- [ ] Run `uv run --all-packages pytest workloads/reckoner/tests/test_v1_history.py workloads/reckoner/tests/test_v1_sampling.py workloads/reckoner/tests/test_v1_preparation.py -q`;
  record RED.
- [ ] Implement checksum-first preparation with bounded streaming/external sorting,
  atomic output publication, and resource checks from the parent plan. Keep complete
  histories source-backed; derived indexed history is a bounded working set, not
  a replacement for the archive. Maintain previous-card state across window changes.
  Import canonical history rows and privileged resolution rows into tenant-scoped
  tables, with cutoff-enforcing queries for runtime retrieval.
- [ ] Freeze seed `20260930`; select lowest hashes over `(purpose, transaction_id)`
  in each class/year, 200/1,800 for development/validation, excluding prior pilots
  and late-year unresolved records. Weight each by `N_h/n_h`. Persist exact source
  counts, exclusions, manifests, selected IDs, and artifact hashes. Choose the
  pilot as 2 fraud + 18 legitimate from development membership by a separate hash;
  its results may be reused only for identical frozen requests and are identified.
- [ ] Test insufficient fraud (199) fails without partial output; repeated preparation
  gives identical identity; label flips affect only oracle/sampling products, never
  canonical transaction content; time-zone-aware comparisons reject naive inputs.
  Assert development and validation IDs are disjoint and neither intersects 2019.
- [ ] Rerun focused tests and original cohort/adapter/artifact tests. Perform the
  actual no-provider preparation, verify inventory counts against the parent plan,
  and record any strict-adapter discrepancy before accepting the manifests. Store
  generated data in ignored `artifacts/phase3/data/`. Review and commit
  `feat: prepare time-valid Reckoner evidence and cohorts`.

## Task 3 — Comparable evidence in Postgres/pgvector and Neo4j/GDS

**Files:** create `workloads/reckoner/src/reckoner/v1/evidence/{__init__,features,indicators,postgres,neo4j,assemble}.py`,
`workloads/reckoner/src/reckoner/v1/evidence/cypher/{constraints,import,neighbourhood,project}.cypher`,
`workloads/reckoner/src/reckoner/storage/migrations/007_v1_vectors.sql`,
`workloads/reckoner/tests/{test_v1_features,test_v1_indicators,test_v1_retrieval,test_v1_graph}.py`,
`infra/compose.reckoner-v1.test.yaml`; modify Reckoner package metadata/lock,
test fixtures, compatibility notes, and Postgres CI image as required for pgvector.

**Interfaces:** `fit_scaler(development: list[dict]) -> dict`;
`feature_vector(transaction: dict, history: dict, scaler: dict) -> list[float]`;
`risk_indicators(transaction: dict, history: dict, neighbours: dict) -> list[dict]`;
`PostgresEvidence(dsn).for_task(task: dict, config: dict) -> dict`;
`Neo4jEvidence(driver).for_task(task: dict, config: dict) -> dict`;
`assemble_evidence(task: dict, config: dict, relational: PostgresEvidence, graph: Neo4jEvidence | None) -> dict`.
Task documents carry canonical transaction and fixed query timestamp; adapters
return schema-valid evidence with actual cutoff, coverage, and missing reasons.

- [ ] Confirm compatible, freely usable Neo4j/GDS artifacts and Postgres17/pgvector
  image for the host architecture; pin digests and plugin checksum. Start only
  disposable test stores. If licensing/architecture invalidates the proposed pair,
  record and resolve the compatibility gate before graph implementation.
- [ ] Write RED tests for past/future/equal-time edges and resolutions, cross-tenant
  shared merchants, source record ordering, snapshots later than query time,
  empty versus unavailable evidence, same-distance tie-breaking, and scaler leakage.
  Assert future insertion leaves earlier evidence hashes/vectors unchanged.
  Run `uv run --all-packages pytest workloads/reckoner/tests/test_v1_features.py workloads/reckoner/tests/test_v1_indicators.py -q`.
- [ ] Implement an 11-dimensional vector: four `log1p` numeric features (USD amount,
  prior 24-hour count, prior 30-day mean USD amount, seconds since prior card use),
  five channel indicators (`chip`, `swipe`, `online`, `other`, `unknown`), and two
  missing-history flags for mean/time-since. Scale numeric values by development-
  weighted median/IQR, using denominator 1 for zero IQR; missing numbers map to 0
  after scaling and set their flag. Unknown coverage is unavailable, not zero count.
  Exact pgvector L2 search returns top five eligible cases, ties by transaction ID.
- [ ] Fix indicator methods in version `risk-indicators-v1`: amount ratio >=3 with
  at least five prior 30-day card purchases (severity `min(ratio/10,1)`); prior
  24-hour card count >=5 (severity `min(count/20,1)`); shared-merchant resolved-fraud
  exposure with at least 20 eligible resolved cases and at least one fraud
  (severity `min(5*fraud_count/resolved_count,1)`). Rank severity descending then
  indicator ID. Every factor cites exact observed counts/window/source references;
  no factors are inferred from missing history or centrality alone.
- [ ] Implement 30-day query neighbourhoods and 90-day eligible resolved-case search.
  Bound UI results to 100 nodes/200 edges while preserving total counts and a
  truncation flag. GDS uses the preceding UTC day boundary and strictly earlier
  edges; include tenant-scoped account/card/merchant nodes, card-merchant count
  weights, ownership, and explicit shared-merchant identity links. Store algorithm
  projection metadata separately by tenant. Run Louvain and PageRank with pinned
  parameters, single-thread execution, and seed where the chosen algorithm supports
  it. Use Louvain default resolution, PageRank damping .85/max20/tolerance1e-7;
  record convergence. Drop only the task-owned in-memory projection after persisting
  its receipt; retain source-backed histories.
- [ ] Run paired SQL/Cypher tests asserting exact eligible result equality and real
  pgvector top-five results. Commands:
  `uv run --all-packages pytest workloads/reckoner/tests/test_v1_retrieval.py -m integration -q`
  and `uv run --all-packages pytest workloads/reckoner/tests/test_v1_graph.py -m neo4j_integration -q`.
  A missing Neo4j service must fail that integration job, not silently skip it.
- [ ] Benchmark one bounded real projection without providers, record memory/coverage,
  and verify all 1,441-user scope fits before the full experiment. If it cannot,
  request the spec-required scope revision. Review and commit
  `feat: add temporal graph and vector evidence`.

## Task 4 — Jev attempts and provider-wide budget accounting

**Files:** create `workloads/reckoner/src/reckoner/v1/providers/{__init__,jev}.py`,
`workloads/reckoner/src/reckoner/v1/storage/{attempts,budget}.py`,
`workloads/reckoner/src/reckoner/storage/migrations/008_v1_provider_budget.sql`,
`workloads/reckoner/prompts/jev-choice-v1.json`,
`workloads/reckoner/config/jev-prices-v1.json`,
`workloads/reckoner/tests/{test_v1_jev,test_v1_budget}.py`; modify v1 CLI and
compatibility notes. Do not modify the v0 Anthropic request implementation.

**Interfaces:** `JevClient(api_key: str, *, transport=None).evaluate(request: dict) -> dict`
performs exactly one HTTP attempt. `score_task(repo: V1Repository, client: JevClient,
task: dict, evidence: dict, protocol: dict) -> dict` owns validation, reservation,
bounded retry, and response persistence. `ProviderBudget(connection).reserve(call:
dict, maximum: Decimal, protocol: dict) -> dict`; `settle(call_id: str, usage:
dict | None, cost: Decimal | None) -> None`. Reservations include tenant, provider,
run/task, purpose, call ID, maximum, and immutable protocol ID.

- [ ] Write request/response tests using injected HTTP transport: correct auth header
  without logging it, explicit `jev-1.13.0`, binary alternatives, probability sum
  tolerance 1e-6, NaN/bool/out-of-range rejection, unexpected model, usage absent,
  HTML/error body, 401, 422, 429 with Retry-After, 529, connect failure, and ambiguous
  read timeout. Assert state contains only allowlisted canonical/evidence fields;
  arbitrary source keys and oracle data cannot be serialized.
- [ ] Run `uv run --all-packages pytest workloads/reckoner/tests/test_v1_jev.py -q`;
  confirm RED. Implement a synchronous direct HTTP adapter with no library retries,
  5-second connect/30-second response timeout, and safe categorized errors. Persist
  bodies only in protected operational records. Read `JEV_API_KEY` explicitly.
- [ ] Implement maximum three attempts, sequential concurrency initially. Honor
  Retry-After up to 60 seconds or defer the task; exponential backoff 1/2 seconds
  plus seeded/injected jitter for tests. No retry on invalid request/auth; ambiguous
  delivery remains uncertain and requires reconciliation before a new billed attempt.
  Rate limiting and circuit breaking are persisted per provider: five consecutive
  transient failures open the circuit for 60 seconds; one half-open probe, success
  closes it. A skipped call produces degraded status, not a fake provider result.
- [ ] Write transactional tests: two workers attempting the last USD .01 cannot both
  reserve it; retries count separately; uncertain cost retains maximum; actual cost
  above reservation blocks further dispatch; unknown pricing prevents dispatch.
  For 2,000 input tokens at `.042`/million, assert `Decimal('0.000084')` cost.
  Jev totals never consume Anthropic credits; Anthropic totals include the original
  `.493151` ledger exactly once. Use the existing global advisory lock `732019102`
  when reading legacy and v1 Anthropic liabilities. Do not allow concurrent legacy
  dispatch during Phase 3 measurement; the operational runbook enforces that boundary.
- [ ] Implement additive attempts/reservations and immutable settlement records.
  Both legacy-ledger provenance and new entries must exist in the measurement
  database; an empty fresh ledger cannot claim the old balance was preserved.
  Persist response and settlement before workflow checkpoint advancement. SDK-level
  retries stay disabled. `reckoner v1 score --manifest PATH --protocol PATH --env-file PATH`
  checks the protocol's exact request/case/model bounds before dispatch.
- [ ] Run focused RED/GREEN tests, disposable concurrent Postgres budget tests and
  original budget/provider regressions. Verify no provider key is required for the
  test suite. Review and commit `feat: add budgeted Jev scoring attempts`.

## Task 5 — Weighted calibration and reproducible diagnostics

**Files:** create `workloads/reckoner/src/reckoner/v1/calibration/{__init__,fit,metrics,report}.py`,
`workloads/reckoner/tests/test_v1_calibration.py`; modify v1 CLI and experiment docs.

**Interfaces:** `fit_calibration(development: list[dict]) -> dict`;
`apply_calibration(raw_probability: Decimal, artifact: dict | None) -> Decimal`;
`calibration_metrics(rows: list[dict]) -> dict`;
`write_calibration_report(development: Path, validation: Path, output: Path) -> dict`.
Rows contain tenant/user/case identity, probability, label, and stratum weight;
only the evaluator process joins labels. CLI:
`reckoner v1 calibrate --development PATH --validation PATH --output NEW_PATH`.

- [ ] Pin tests for the spec's bucket edges, p=0/p=1, empty bins, one-class samples,
  weighted metrics, user-cluster bootstrap, and unchanged raw probability. For
  `(p=.2,y=0,w=1)` and `(p=.8,y=1,w=3)`, assert weighted Brier `.04`, weighted mean
  probability `.65`, observed rate `.75`, and effective n `1.6`. Identity calibration
  returns the exact input Decimal, including 0 and 1.
- [ ] Run `uv run --all-packages pytest workloads/reckoner/tests/test_v1_calibration.py -q`;
  confirm RED. Implement all spec metrics with per-tenant populations and availability
  statuses; write JSON and a readable Markdown report plus a standalone reliability
  plot. Do not make graph or note-eval success depend on prettifying this plot.
- [ ] Fit monotonic logistic map with SciPy L-BFGS-B, `a>=0`, clipped input
  `[1e-6,1-1e-6]`, starting `(a,b)=(1,0)`, maximum 1,000 iterations, and weighted
  mean log-loss plus `.001 * ((a-1)^2 + b^2)`. Record convergence and exact versions.
  Use 1,000 user-cluster bootstrap replicates, seed `20260930`, 2.5/97.5 percentiles.
  Empty/one-class replicates are reported; unestimable intervals remain unavailable.
- [ ] Select the fitted candidate only when validation Brier is lower and log loss
  is no higher than identity; retain raw otherwise. Freeze the decision report;
  sparse-bin warnings and observed 2017/2018 prevalence drift remain visible even
  when the numeric selection rule succeeds. Test validation-label perturbation
  cannot change the fitted coefficients, only validation metrics/selection.
- [ ] Reject a calibration artifact used with another model, question, feature/scaler,
  or evidence mode. Test NaN, unconverged fits, missing stratum counts, class-weight
  reuse across years, and attempted 2019 training inputs. Run focused tests and Ruff;
  review and commit `feat: report weighted scorer calibration`.

## Stage A exit

Offline/unit and actual-store tests pass; prepared manifests and graph resource
receipts exist; no paid scoring claim is made without its protocol/result artifacts.
Stage B can proceed with explicit test transports while paid gates remain pending.
The final experiment cannot proceed until real scoring/calibration evidence exists.

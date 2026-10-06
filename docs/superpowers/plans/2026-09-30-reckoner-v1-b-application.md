# Reckoner v1 — Workflow and Reviewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run resumable Reckoner decisions and evidence-backed notes, and give human and simulated reviewers usable APIs and UI.

**Architecture:** LangGraph orchestrates versioned, tenant-scoped work using the stage A evidence and Jev contracts. Postgres and a durable outbox own operational state; FastAPI and the existing Next.js app serve review and configuration workflows.

**Tech Stack:** Python 3.12, LangGraph durable Postgres checkpointer, existing Postgres/pgvector and FastAPI, Anthropic through LiteLLM, DeepEval/Ragas, Next.js 16/TypeScript.

**Spec:** [Approved Phase 3 specification](../../spec/004-reckoner-v1.md).

## Global Constraints

All [parent-plan constraints and gates](2026-09-30-reckoner-v1.md) apply. This
stage implements Tasks 6–9 on the shared isolated branch. No live provider call
is authorized by these software tasks. Provider and judge clients use injected
deterministic transports in tests. API readers never access oracle labels.

## Review Focus

Parent risks 1, 3, 4, and 5 map to Tasks 6–9. Tests specifically cover a response
persisted before a worker crash, repeated/crossed reviewer writes, settings changes
during a pinned run, delayed note cost settlement, hostile evidence content, and
opaque identifiers.

## Task 6 — Durable LangGraph run and routing

**Files:** create `workloads/reckoner/src/reckoner/v1/workflow/{__init__,graph,nodes,state,runner}.py`,
`workloads/reckoner/src/reckoner/v1/storage/checkpoints.py`,
`workloads/reckoner/src/reckoner/storage/migrations/009_v1_workflow.sql`,
`workloads/reckoner/tests/{test_v1_graph_workflow,test_v1_resume,test_v1_routing}.py`,
`docs/architecture/reckoner-v1-workflow.mmd`; modify v1 storage repository,
package metadata, lockfile, and test markers only as needed.

**Interfaces:** `build_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph`;
`run_task(repo: V1Repository, graph: CompiledStateGraph, task: dict, config: dict) -> dict`;
`route(probability: Decimal, amount: Decimal, thresholds: dict) -> dict`;
`resume_task(repo: V1Repository, graph: CompiledStateGraph, tenant_id: str, run_id: str, task_id: str) -> dict`.
Graph state carries tenant/run/task/transaction IDs, immutable config/evidence IDs,
provider call IDs, score/decision IDs and statuses, checkpoint version, and bounded
error categories. It never carries oracle labels or full histories.

- [ ] Write RED routing tests for `.004`, `.005`, `.05`, `.90`, `.901`, flat low
  `.05`, amount-derived low thresholds for USD 20/400/4,000, zero/negative amount,
  missing probability, and invalid thresholds. Pin equality to escalation and the
  three allowed outcomes.
- [ ] Write RED workflow tests for evidence unavailable → degraded escalation,
  valid raw/calibrated probability → routing, auto decision → persisted terminal
  state, escalation → case creation, and duplicate node/task execution.
  Run `uv run --frozen --all-packages pytest workloads/reckoner/tests/test_v1_routing.py workloads/reckoner/tests/test_v1_graph_workflow.py -q`.
- [ ] Implement pure `route()` with `Decimal`; never call Jev inside the router.
  Validate amount/currency and threshold order. Persist decision identity and
  snapshot before exposing success; duplicate same-payload writes return existing
  records, conflicting records fail visibly.
- [ ] Add a persistent Postgres checkpointer using the supported LangGraph
  checkpoint interface. Scope checkpoint lookup and writes by tenant/run/task and
  pin the serialized config/evidence IDs at task creation. Use the LangGraph
  diagram generator for the committed workflow view.
- [ ] Add crash/resume regression: a Jev response and billing settlement committed
  before checkpoint advancement are consumed after restart without another HTTP
  request; an ambiguous provider state stays uncertain; repeated completed resumes
  return the same decision. Altered run settings after start do not affect state.
- [ ] Test retries do not double-count decisions/cost, no oracle field enters graph
  state, run/task IDs containing `/` remain opaque, checkpointer tenant mismatch
  fails, and provider failure routes to degraded escalation. Run targeted tests,
  Ruff, LangGraph serialization compatibility, and original baseline regressions.
  Independent review, then commit `feat: add durable Reckoner v1 workflow`.

## Task 7 — Structured case notes and evaluation gates

**Files:** create `workloads/reckoner/src/reckoner/v1/notes/{__init__,generate,validate,prompt}.py`,
`workloads/reckoner/src/reckoner/v1/evaluation/{__init__,notes,judges,report}.py`,
`workloads/reckoner/prompts/case-note-v1.md`,
`workloads/reckoner/tests/{test_v1_note_generation,test_v1_note_eval,test_v1_judges}.py`,
`docs/operations/note-evaluation.md`; modify task declarations and workflow nodes.

**Interfaces:** `generate_note(case: dict, evidence: dict, score: dict, client: object, config: dict) -> dict`;
`validate_note(note: dict, evidence: dict, score: dict) -> dict`;
`evaluate_note_fixtures(cases: list[dict], judges: dict, expected_count: int) -> dict`.
Evaluation clients are injected; `evaluate_note_fixtures()` cannot dispatch unless
passed an explicitly approved protocol and a budget-reservation implementation.

- [ ] Write RED schema tests for exactly six fields, approve/decline note verdict,
  Jev confidence provenance, at most three ranked indicators, evidence references,
  explicit empty/missing results, note status, and strict unknown-key rejection.
  Assert unsupported claims, invented confidence, malformed evidence IDs, invalid
  JSON, and refusal/failure cannot produce a valid note.
- [ ] Write RED provider tests for prompt input allowlisting, no oracle/raw card
  details, correct model/prompt version, output parsing, empty content, refusal,
  timeout, bounded schema-repair attempt, and exact generation usage attribution.
  Run `uv run --frozen --all-packages pytest workloads/reckoner/tests/test_v1_note_generation.py workloads/reckoner/tests/test_v1_note_eval.py -q`.
- [ ] Implement note prompts and deterministic validation. Structured values and
  evidence summaries are passed as data, never interpolated into trusted instructions.
  Each note indicator and neighbourhood/comparable claim must cite supplied
  evidence. Notes cannot add feature attribution or synthesize a probability.
- [ ] Persist escalation before note dispatch. Successful valid note, missing note,
  invalid note, and uncertain billing remain distinct records. A case stays
  reviewable when generation fails. At most one schema repair is allowed per
  separately reserved attempt; then retain failure status.
- [ ] Implement evaluation adapters matching spec semantics: schema validity over
  all expected notes; DeepEval note-only verdict agreement against evaluator-only
  oracle joins; Ragas faithfulness against exact note evidence. Persist per-case
  pass/fail/error/not-evaluated and judge/model/prompt/library provenance. Missing
  results and errors block a passing aggregate; empty populations are not evaluated.
- [ ] Use static fake judges only for unit/integration tests. Tests assert hidden
  oracle and full transaction data never enter judge input; each evaluation run
  covers all expected cases; `.90` agreement and `.90` mean faithfulness boundaries
  are inclusive only when errors/missing cases are zero. The later approved pilot
  determines paid compatibility and API behavior, not fabricated CI receipts.
  Review, run focused tests and offline regressions, then commit
  `feat: add structured evidence-backed case notes`.

## Task 8 — Reviewer/configuration operational API

**Files:** modify `workloads/reckoner/src/reckoner/api.py` and add
`workloads/reckoner/src/reckoner/v1/api/{__init__,cases,reviews,configurations,schemas}.py`,
`workloads/reckoner/src/reckoner/v1/storage/{cases,reviews,configurations}.py`,
`workloads/reckoner/src/reckoner/storage/migrations/010_v1_operational_api.sql`,
`workloads/reckoner/tests/{test_v1_cases_api,test_v1_reviews_api,test_v1_config_api}.py`.

**Interfaces:** `list_cases(repo, *, tenant_id: str, run_id: str | None, status: str, limit: int, cursor: str | None) -> dict`;
`submit_review(repo, action: dict, *, idempotency_key: str, expected_version: int) -> dict`;
`create_configuration(repo, document: dict) -> dict`;
`activate_configuration(repo, *, tenant_id: str, config_id: str) -> dict`;
`preview_configuration(repo, *, tenant_id: str, run_id: str, config_id: str) -> dict`.
The authenticated API surface follows existing local-demo identity conventions;
the request-supplied human actor ID is attribution only and is not authenticated
identity or tenant isolation. Simulated identities are accepted only by the
internal simulation command, never as reviewer API actors.

- [ ] Pin API tests for tenant/run/case opaque IDs including slashes, limit/cursor
  validation, case status filters, unavailable graph, missing note, degraded status,
  and response schemas. Assert API projections cannot expose oracle labels, secrets,
  raw provider requests/responses, or unbounded graph data.
- [ ] Pin review transaction tests: first approve/decline atomically records action,
  version transition, and OTLP outbox event; same idempotency key returns original;
  stale expected version conflicts; two concurrent reviewers yield one winner;
  a human identity cannot impersonate the simulated reviewer.
- [ ] Pin configuration tests: content ID determinism, no mutation/deletion of saved
  versions, tenant-scoped activation, historical run pinning, model registry and
  model/calibration qualification, invalid price/model rejection, and score-preview
  no-provider/no-cost behavior. Use explicit fake registry entries for unsupported
  models and assert rejection.
- [ ] Implement read endpoints `/v1/cases`, `/v1/case`, `/v1/configurations`, and
  `/v1/configurations/preview`; write endpoints `/v1/reviews`, configuration create,
  and activate. IDs travel as validated query/body fields, never unescaped route
  segments. Reuse the existing FastAPI app and separate DB role capabilities.
- [ ] Use row-level version comparison in one transaction for review writes; insert
  outbox with deterministic event identity in that transaction. Add API role grants
  only for reviewed views and explicit review/configuration procedures. Verify
  owner, runner, reviewer API, and evaluator oracle privileges with disposable
  Postgres integration tests.
- [ ] Run route, permissions, migrations, and existing API/storage regressions.
  Confirm OpenAPI output does not contain secret-bearing request types. Independent
  review, then commit `feat: expose Reckoner review and configuration APIs`.

## Task 9 — Reviewer console and settings surfaces

**Files:** create `web/app/review/page.tsx`, `web/app/settings/page.tsx`,
`web/components/review/{case-queue,case-detail,structured-note,graph-viewer,review-actions}.tsx`,
`web/components/settings/{configuration-editor,configuration-history,configuration-preview}.tsx`,
`web/lib/reckoner-api.ts`, `web/lib/reckoner-types.ts`, `web/tests/reviewer.spec.ts`,
`web/tests/settings.spec.ts`; modify `web/app/page.tsx`, existing shared navigation,
and `web/tests/fixture-api.mjs`.

**Interfaces:** typed client functions `listCases(filters)`, `getCase(tenantId, caseId)`,
`submitReview(action, idempotencyKey, expectedVersion)`, `listConfigurations(tenantId)`,
`createConfiguration(document)`, `activateConfiguration(tenantId, configId)`,
and `previewConfiguration(tenantId, runId, configId)`. All requests use the
existing server-side `TOUCHSTONE_API_URL` plus `RECKONER_API_URL`; no browser token.

- [ ] Add Playwright fixtures for open, resolved, stale-version, note-failed,
  degraded, no-graph, empty, and large/crowded queues; verify fixtures include no
  oracle fields. Write RED queue navigation tests for `j`/`k`, `a`/`d`, control
  focus exclusion, focus visibility, and no duplicate action on repeated keypress.
- [ ] Write RED detail tests for six note fields, rank/source links, graph cutoff,
  cross-tenant identity label, missing vs empty evidence, note failure, provider
  degradation, and visible simulation provenance. Verify label/oracle never appears
  before human submission. Use text-only dangerous values to prove React renders
  evidence as text, not executable markup.
- [ ] Write RED settings tests for six thresholds, scorer/model fields, immutable
  history, selected configuration, labelled score-only preview, equality at route
  boundaries, disabled unsupported model, and activation affecting future runs only.
- [ ] Implement accessible dense responsive console and settings using existing
  Next.js patterns, no new component framework. The graph view renders bounded
  nodes/edges using safe SVG/text labels and explicit empty/unavailable/truncated
  states. Avoid network fetches for provider keys.
- [ ] Run Playwright desktop/mobile targeted specs, `npm run lint`,
  `npm run typecheck`, `npm run format:check`, and `npm run build` in `web/`.
  Verify existing dashboard tests/build still pass. Review visual failures, correct
  them in this task, obtain independent code review, then commit
  `feat: add Reckoner reviewer and settings console`.

## Stage B exit

Offline workflow/storage/API/web tests pass with no provider secrets. Restart,
concurrency, and note-failure behavior are evidenced. Paid model and judge claims
remain pending until their bounded protocols and runs are separately approved.
Stage C can measure deterministic fixtures and prepare gated protocols.

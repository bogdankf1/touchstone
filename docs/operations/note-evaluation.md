# Structured note generation and evaluation

All workload data is simulated. Offline injected responses verify software interfaces,
not note quality or provider compatibility on a paid run. The measured quality gate
remains pending a separately approved experiment. Haiku is both the initial note model
and judge; agreement between them is not independent human validation.

## Runtime and storage

`V1Repository(..., note_client=NoteProvider(...), note_protocol=approved_protocol)`
attaches runtime-only note execution. Supply credentials directly to the client from
the authorized runtime; neither this API nor evaluation discovers credential files.
`run_task()` and `resume_task()` keep the original Task6 settings and immutable scoring
bindings. An escalation atomically creates `v1_note_work` before any note dispatch.
No client or protocol leaves it pending. Later attachment resumes it even when the
LangGraph decision is already complete. The existing task lifecycle lock protects
continuation. `continue_note()` is an internal helper called under that lock.

`build_note_context(decision, evidence, score, frozen_config)` preserves separate
`routing.raw_probability`, the persisted `routing.effective_probability`, frozen
score mode and calibration ID, alongside unchanged Jev confidence. It never edits
the original provider response. `build_note_request(decision, evidence, score,
frozen_config)` serializes that context and prepares the exact request for approval
after routing. `generate_note(case, evidence, score, client,
config, *, protocol=None)` uses an injected `BudgetedCalls`, not an unmetered model.
`BudgetedCalls(repo, provider, task, config, kind="note"|"judge", budget=None)`
uses the Task4 `ProviderBudget` and exact request hashes. An optionally shared budget
must use the same connection. Read-only preflight validates the frozen model, prompt,
pricing, request shape, identity, purpose, token bounds and protocol before a stage
can become immutable. Validation failures cannot pin an unusable stage.

Migration011 adds tenant-scoped, immutable note declarations, results, generation
stages and evaluation artifacts. It does not edit migrations005–010. Actual calls and
settlements still live in `v1_provider_calls`, `v1_settlements` and
`v1_provider_responses`; the latter's `score` JSON stores the generation attempt
result for Anthropic calls. Its existing Jev documents remain unchanged. Each
`v1_generation_stages` row links a task/stage to exactly one call. A session advisory
lock prevents duplicate dispatch. Response and settlement commit atomically; a restart
reuses both. A reserved call without a response becomes uncertain, with no automatic
redispatch or zero-cost refund.

`note_work(repo, decision)` returns the terminal result or its pending declaration.
Results distinguish succeeded, invalid, failed and uncertain; each attempt separately
records settled/uncertain billing, usage, price, protocol and timestamps. A valid note
with uncertain usage remains readable but cannot imply complete online cost. Exhausted
invalid content can have fully known cost and still fail quality. Root decision timing
is unchanged; readiness comes from the note's actual completion timestamp.

A case remains reviewable throughout. The note writer locks the case through a narrow
row-lock-only security-definer function, without gaining case UPDATE access. If review
already completed, the late note remains in the result with
`available_before_review=false` and is not inserted into the pre-review note view.
Operational review code must capture its recommendation in the same case-row-locked
transaction and retain that immutable action snapshot.

## Content contract

Six content fields plus strict provenance/status metadata are required. New notes use
`confidence.meaning=jev_distribution_concentration` and copy `score.confidence`
exactly. Neither raw nor calibrated fraud probability is confidence or accuracy.
Missing/failed scoring yields unavailable confidence and a degraded note. Legacy
schema enum spellings remain readable for compatibility, but the new validator rejects
them as confidence provenance.

Instructions are fixed; allowlisted evidence is JSON in the user message. No raw
transaction/card details or current oracle enters generation. Indicators must copy
supplied descriptions, methods and references; rank is ordering, not feature
attribution. Neighbourhood and comparable summaries use deterministic structured
serializations. Explicit empty arrays and an unavailable neighbourhood are valid.
Proposed review actions cite supplied evidence. Deterministic checks reject unsupported
structured facts/references; Ragas assesses remaining semantic faithfulness.

## Bounded actual calls and cost linkage

The model is `anthropic/claude-haiku-4-5-20251001`, temperature0, timeout30 seconds.
LiteLLM has both retry settings zero; no automatic transport or authentication retry.
Input is bounded conservatively by serialized UTF-8 request bytes and by the frozen
configuration/protocol ceiling (Task4 ceiling at most32000). Output is bounded by the
protocol, frozen config and transport limit4096. Each reservation uses pinned USD
1/M input and5/M output pricing; cache usage or missing/incompatible usage remains
uncertain. These are usage-priced estimates, not invoices.

| Logical work | Actual stages | Maximum calls per case |
|---|---|---:|
| Note | `note-generation`, optional `note-repair` | 2 |
| DeepEval verdict | `judge-verdict` | 1 |
| Ragas faithfulness | `judge-statements`, `judge-faithfulness` | 2 |

A note repair resends the same approved request once. It must use the original note
protocol's remaining attempt allowance; maximum_attempts1 disables repair. Provider
failure/refusal is not a schema repair. Every dispatched stage reserves independently.
Maximum per-case token bounds are2× each note input/output ceiling and3× each judge
ceiling, further constrained by protocol USD caps. Claim-free Ragas output stops after
its first call and cannot pass. All Ragas statement verdicts must match the extracted
statement list exactly. There are no parser/Instructor/framework retries.

Calls carry purpose `online-note` or `judge`, plus source_run_id/source_task_id and
stage. All note/repair costs belong to the original online logical task. Judge costs
remain visible separately and outside online CPST; Task10 supplies the generic
online/offline cost-scope representation. Do not equate completed decisions with
complete note work or settled online billing.

## Evaluation interfaces and staged authorization

Pinned supported interfaces are DeepEval4.2.7 `BaseMetric`/`LLMTestCase` and Ragas0.4.3
`metrics.collections.Faithfulness`/`InstructorBaseRagasLLM`. Ragas requires
`langchain-community==0.4.1`:0.4.2 removed a module that Ragas imports. Joint resolution
and real adapter execution are tested. Runtime disables framework telemetry, update
checks and DeepEval dotenv discovery before importing frameworks. Pytest disables the
DeepEval publishing plugin and pytest-rerunfailures plugin in repository configuration.

`evaluate_note_fixtures(cases, judges, expected_count, *, protocol=None, budget=None)`
accepts `judges={"verdict": ..., "faithfulness": ...}`. Each value may be an adapter or
a factory receiving only `{tenant_id,case_id}` and returning that case's adapter.
Factories let multi-case runs bind each actual call to its own task. Build each adapter
with the same supplied protocol and `BudgetedCalls(..., kind="judge", budget=budget)`.
Case records include identity, note, evidence, score and an evaluator-only
`oracle_verdict`; arbitrary raw fields are never forwarded. Fixture-only evaluation
also supplies decision/config documents. Durable evaluation loads those documents
from the persisted decision and frozen configuration; caller replacements cannot
select different routing inputs. DeepEval's custom metric
sees the note, then compares the returned verdict locally with the expected verdict;
it never sends `expected_output` to the model. Ragas receives the six-field note and
`RagasFaithfulness.evaluate(note, *, context=...)` receives the identical frozen
generation context, including the effective routing probability and calibration ID.
There is no raw-probability-only fallback. Oracle joins belong to an evaluator connection,
not the runner, scorer, browser or provider.

With a real `ProviderBudget`, evaluation requires all root tasks complete and exact
membership of every persisted escalation in the tenant/run. Before dispatch, a tenant-scoped join verifies
the actual note-work declaration/result, published note, decision, evidence, original
scorer response and configuration. Supplied note/evidence/score documents must match
those immutable records exactly. The saved generation call request must also match
the reconstructed request; context or prompt drift blocks evaluation. Pending/failed
notes cannot be replaced with caller-created valid-looking notes. All cases, including
missing/degraded notes, remain in the denominator. Results include case status,
provenance, evidence/note IDs, individual scores and failures; reports persist in
`v1_note_evaluations`. Schema validity must be100%; agreement and mean faithfulness
must each be at least.90 inclusively. Missing/error/scoreless results block passing;
zero cases are not evaluated. Fixture gate arithmetic never constitutes measured
quality acceptance.

Current exact-hash protocols can be supplied as
`{"approved":true,"stages":{"judge-verdict": protocol,...}}`. Each nested protocol
is independently validated/reserved. Ragas's second prompt depends on its persisted
first response. An unapproved stage raises `ApprovalRequired` carrying the exact safe
request/stage/hash; the evaluator records this as not evaluated. Re-running with that
stage approved reuses earlier saved calls. No request hash is guessed or relaxed.
For multiple cases, each stage protocol lists every corresponding exact task request.

This staging is a safe compatibility interface, not a permanent requirement for the
owner to approve every stage separately. Task13 owns the concrete bounded pilot
protocol and can introduce a versioned derived-request policy bound to fixed templates,
prior response hashes, stages, cases, models, token limits and total cap. Scoring
approval never implicitly authorizes notes/judges. Actual original ledger restoration,
paid compatibility and release-quality acceptance remain Task13 responsibilities.

Primary interface references:
[DeepEval custom metrics](https://deepeval.com/docs/metrics-custom) and
[Ragas metric interfaces](https://docs.ragas.io/en/v0.4.3/references/metrics/).

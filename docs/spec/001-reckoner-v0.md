# 001 — Reckoner v0 baseline

**Status:** Proposed for review; implementation has not started.

**Date:** 2026-09-25

**Parent:** [Approved foundation specification](000-foundation.md), especially §§4–8 and Phase 1.

## 1. Purpose and agreed decisions

Produce a reproducible, measured baseline for Touchstone: one Anthropic decision call for each
of 1,000 frozen simulated CCTD transactions, with durable results, honest cost/quality metrics,
and replayable OTLP traces. This phase succeeds by producing trustworthy evidence, regardless
of whether the model performs well. No production or real-customer claim is permitted.

The human has confirmed these refinements:

- Interpret source dollar amounts as USD and source wall-clock timestamps as UTC. These are
  experimental assumptions, not verified properties of the generator. Record both in provenance.
- Start with Claude Haiku 4.5 through LiteLLM. Keep model selection in a versioned configuration
  file now; expose it in the shared Admin UI when that surface is scheduled. A change affects
  new runs only. Record the requested and provider-reported model for each call.
- For v0, a provider failure remains failed/incomplete. Preserve known charges and the fixed
  cohort denominator. Do not manufacture a successful escalation. Automatic fallback escalation
  in foundation §5.4 applies to v1; this explicitly resolves its ambiguity for v0.
- Keep the existing phase schedule. No frontend, Jev integration, graph, retrieval, or case-note
  generation is added to Phase 1.

The existing initial $10 provider budget includes pilots, baseline calls, and any later judges.
The local `.env` supplies credentials; configuration, reports, prompts, and telemetry never
contain secrets. Presence of a key is not proof of working access or funded credit.

## 2. Recommended execution shape

Use a command-line batch runner backed by Postgres, sharing the Reckoner package and container
image with FastAPI. The API exposes health/readiness and tenant-scoped read-only run/results
endpoints. Starting measured runs is an explicit CLI operation in this phase.

This is preferred to two alternatives: an in-request FastAPI batch would make long runs depend
on HTTP connection lifetimes; adding a queue service and distributed workers would introduce
deployment and recovery machinery beyond this sequential 1,000-case experiment. A durable
single-runner design supplies recovery without adding another service.

The flow is source validation → frozen cohort → Postgres import → budget reservation → model
call → durable result/usage → oracle evaluation → report and OTLP export. Evaluation can run
again from stored results without calling the provider. Prompt construction has no oracle access.

## 3. Data preparation and cohort

Preserve the source archive unchanged. Reuse Phase 0 checksums, checking the actual files before
freezing artifacts. Stable transaction identity derives from the source checksum and one-based
CSV data-record ordinal, excluding the header. Identical-looking rows remain distinct records.
Card identity includes its user reference; merchant identity preserves the source merchant key.

Map only observed transaction fields into `transaction-v1`. Parse money through decimal/integer
arithmetic. Append explicit USD/UTC assumptions to normalization provenance. Country, devices,
and IPs remain unknown when absent. No row-position join to `sd254_users.csv` is permitted;
current-age, FICO, dark-web, and other unverified historical attributes are excluded.

Keep negative/zero amounts and invalid records in source/history coverage and rejection counts,
but exclude them from purchase evaluation. Every record must have a counted disposition.
Raw CCTD field names are confined to the adapter, source diagnostics, and source documentation.

Proposed temporal split:

| Population | Inclusive UTC interval | Use |
|---|---|---|
| Development/history | Source beginning through 2018-12-31T23:59:59Z | Tenant assignment and separate pilot |
| Frozen comparison | 2019-01-01T00:00:00Z through 2019-12-31T23:59:59Z | 100 fraud and 900 legitimate purchases |
| Reserved future data | 2020 onward | Unused in baseline selection/tuning |

Check eligibility counts before finalization. If this interval cannot supply 100/900 eligible
cases, stop and revise the design with the human; do not silently widen it. Future confirmation
data sufficiency is not claimed by reserving 2020.

Freeze seed `20260925`. Assign complete users to tenants using pre-holdout activity only: rank
users by descending pre-holdout fraud fraction, break ties using a seeded hash of user identity,
and choose the prefix whose transaction count is nearest 30% of pre-holdout volume for tenant B.
Tenant A receives the remainder. Users without pre-holdout activity receive deterministic 70/30
hash assignment. Report actual volumes and risk differences in both periods; no exact split or
future risk ordering is promised. Historical labels influence experimental assignment only,
never runtime features or prompts. Tenant identifiers are also excluded from model inputs.

Within each eligible holdout label stratum, select the lowest seeded hashes of stable transaction
IDs, taking 100 fraud and 900 legitimate without replacement. This is evaluation-case selection,
not row sampling of graph histories. Do not impose an extra per-tenant class quota.

Retain complete histories for all users with any source fraud plus all cohort/pilot users.
The immutable original archive can serve as the on-disk history backing: a versioned entity
manifest and checksums identify complete histories without copying the entire dataset or
loading it into Postgres. All 29,757 source fraud rows must remain covered, including unsupported
amounts. A later graph phase must apply per-transaction time cutoffs when reading this backing.

Emit tenant-scoped cohort manifests under the existing contract, a tenant-assignment manifest,
history-entity manifest, normalization metadata, runtime transaction artifacts, and a separate
oracle artifact. Freeze IDs/checksums before scoring. The cross-tenant union must contain exactly
1,000 unique transactions and the declared class counts. Source/history access is available to
preparation, not to the model runner. The runner imports only canonical cohort transactions.

## 4. Configuration and provider behavior

Add a versioned model/run configuration beside the existing threshold configuration. It contains
provider, explicit model ID, prompt version, generation settings, timeout, output-token limit,
and price-table version. Initial defaults are Haiku 4.5, temperature 0, at most 256 output tokens,
a 60-second request timeout, one in-flight call, and no automatic retries. Disable retries in
both LiteLLM and its underlying client. Disable application/provider prompt caching and tools.
Pin a supported concrete Haiku model ID during implementation; aliases must not drift unnoticed.

Resolve configuration once, validate it, and persist its content-addressed snapshot before a run.
Changing a file cannot change a running or historical experiment. Unsupported/unpriced model
configuration fails before dispatch. Future Admin uses the same configuration semantics; this
phase creates no settings UI or credential-editing endpoint.

The prompt receives an allowlist of canonical transaction facts: amount/currency, timestamp,
channel, merchant category/location, and processing errors. Exclude oracle labels, source row
positions, tenant assignment, raw payment credentials, demographic enrichment, and histories.
Ask for exactly one of `auto-approve`, `auto-decline`, or `escalate` in a strict structured response.
The versioned system prompt states the three outcomes and agreed business-cost assumptions;
threshold-config identity remains attached for those assumptions, but v0 executes no score thresholds.
Treat source strings as untrusted data. A brief rationale may be retained as model output, but
is not a validated case note, calibrated score, or evidence of correctness.

Invalid/truncated responses, timeouts, rate limits, and provider errors produce failed attempts.
No repair call, alternate-model fallback, or rules decision replaces them. A failed task stays
in the fixed cohort and can make the run incomplete. A new experimental retry requires a new
run identifier and consumes the same remaining global budget.

## 5. Persistence and recovery

Use Postgres operational tables for canonical transactions, cohort membership, immutable run
configuration, runs/tasks, provider attempts and budget reservations, decisions, oracle labels,
evaluation outcomes, and pending telemetry. All application rows carry `tenant_id`; shared
configuration versions have tenant-scoped records. Composite constraints and joins enforce
tenant ownership. Schema migration bookkeeping is infrastructure metadata.

The model runner's database role cannot read oracle tables; the evaluator can. The API does not
return oracle labels through runtime transaction/run endpoints. This is a concrete leakage
boundary, not an SSO/RBAC product. Persist only the cohort/pilot operational rows and results;
complete source histories remain on disk. Vector search and embeddings are deferred.

Before a paid call, durably record its unique identity, start state, and budget reservation.
Afterward persist the response, usage, decision or error, and evaluation/export state. Resume
skips durable completed calls. A crash after dispatch but before response persistence is an
uncertain attempt: retain its reservation and stop for reconciliation, never automatically
reissue it or claim exactly-once external execution. A local database lock prevents concurrent
baseline runners; budget checks also occur in a serialized transaction across both tenants.

## 6. Pilot and budget controls

Use a separate 20-case pre-holdout pilot, stratified 2 fraud/18 legitimate, with the same frozen
prompt/configuration intended for the full run. Pilot costs and results are reported separately;
pilot cases never replace baseline cases. A prompt/config change invalidates extrapolation from
the previous pilot and requires a newly identified pilot within the remaining budget.

Reserve conservative maximum input/output cost before each dispatch using the pinned prices,
bounded prompt, provider-compatible token accounting, and configured output limit. The plan must
specify and test the exact reservation calculation. Unknown pricing or unavailable token bounds
blocks dispatch. Reconcile observed usage afterward; retain uncertain charges as reservations.
Do not release money merely because a request timed out. An unexpected charge above its reservation
stops further dispatch and is reported; the software cannot guarantee the provider's final invoice.

The pilot has a $1 sub-limit inside the shared $10 cap. Before the 1,000-case run, require valid
pilot responses and complete usage, report measured pilot spend/latency, and calculate both
projected spend and a conservative reservation bound for the full remaining workload. Proceed
only if the bound fits remaining funds. Otherwise stop for scope/budget review. Never silently
reduce the cohort, switch models, or reset spend on a new run or process restart.

Model pricing is versioned evidence, not a floating library default. Token-priced observed cost
is labelled as calculated from provider-reported usage, distinct from invoice reconciliation.
Unknown charges make total cost/CPST incomplete. The global budget includes both tenants and
every pilot/run; simulated business losses and review costs do not debit the API budget.

## 7. Measurement and artifacts

Instrument each logical task and provider attempt with OpenTelemetry. Emit existing generic
measurement envelopes for execution, usage, evaluated outcome, evaluation, and metric
contributions. Cost belongs only to the billable-call identity, never duplicated on parent spans.
Use valid OTel trace/span IDs and preserve event IDs during re-export. A measured provider call
on simulated transactions must be distinguishable from a fake-provider test.

Persist replayable OTLP trace requests locally with an export manifest, checksums, and versions.
Use standard OTLP encoding; a console pretty-print is not the interchange format. Durable export
state must make failed exports visible and allow replay without model execution. Test decoding
and delivery to an OTLP test receiver. Phase 2 ingests these through OTLP, never by importing
Reckoner modules or querying its tables. The production collector/ClickHouse pipeline stays in
Phase 2. The plan pins GenAI conventions and documents custom `touchstone.*` attributes.

Keep secrets, full source records, prompts, and raw model response text out of telemetry. Store
allowlisted prompt/response artifacts separately with checksums and provenance for reproducibility.

Reckoner calculates the foundation metric definitions from oracle joins after decisions:
correctness; $4 escalation cost; full amount for missed fraud; 30% of amount for false declines;
CPST; completion coverage; false-positive/missed-fraud/escalation rates; and nearest-rank p99.
Use decimal money throughout. Report aggregate and per-tenant numerators, denominators, and
cost components. Failed cases are not correct and are not removed from rate denominators.

If any task or cost remains incomplete, mark the run incomplete. A partial CPST, where computable,
must identify its completed population and cannot represent the full baseline or a winning result.
Zero correct decisions makes CPST undefined. Report latency for completed tasks with count and
failure counts alongside it. Separate quality correctness from response-schema validity; a valid
but incorrect decision fails the correctness evaluation. Missing/failed decisions are evaluation
errors against all expected cases. Case-note/faithfulness and human-agreement metrics are not
evaluated in this phase and must not appear as passing.

The deliverable is a local JSON/Markdown run report linking run/config/prompt/cohort versions,
attempts, decisions, evaluation results, and trace export. It states the enriched 100/900 mix,
simulated data, ideal-review assumption, assumed USD/UTC, and the absence of population claims.
Generated data/artifacts remain git-ignored; commit only aggregate evidence and small synthetic
test fixtures. An incomplete paid run is disclosed, never replaced by fake results.

## 8. Delivery boundaries and acceptance

Keep the approved top-level repository layout. Source mapping, runner, persistence, business
evaluation, and operational APIs belong under `workloads/reckoner/`; plain schemas and telemetry
conventions under `contracts/`; deployment under `infra/`; specifications and evidence under
`docs/`. No fraud logic enters `platform/`; `web/` remains scheduled for later delivery.

The implementation plan should divide work into these reviewable increments:

1. Canonical adapter and deterministic frozen cohort/history manifests, with leakage tests.
2. Postgres migrations/repositories, immutable run configuration, and resume/budget state.
3. Instrumented LiteLLM baseline runner, strict responses, durable OTLP export, and failure tests.
4. Oracle evaluation, reproducible reports, and read-only operational API.
5. Compose/kind and CI integration, resource/architecture verification, then paid pilot/baseline.

Each increment follows TDD, fresh implementation subagent, and independent review. Follow with
whole-branch review and the finish-branch process. Code lives in a new isolated worktree after
plan approval. Do not carry forward ignored agent scratch files as product artifacts.

Required evidence includes deterministic artifact hashes; exact cohort counts; all-fraud/full-
history coverage; tenant-safe persistence; oracle-inaccessible prompt construction; no silent
retries/double charging; recovery after uncertain dispatch; budget rejection before calls; valid
OTLP round-trip and re-export identities; known-answer metric tests; and incomplete-run reporting.

Run integration tests against real Postgres; use an explicit fake provider only for deterministic
tests and deployment smoke checks. CI makes no paid requests. Smoke-test API/database/run-report
behavior in Compose and kind separately. Preserve measured artifacts and persistent data during
cleanup. Record peak memory/disk and verify the architecture with the required archify drift check.

Phase 1 software readiness and the measured baseline are separate acceptance results. Report
both: working code/tests do not establish that a paid 1,000-case run completed. Provider access,
funds, dataset feasibility, or resource failures must remain explicit blockers.

## 9. Approval and next step

This document extends the approved foundation with Phase 1 decisions and proposed implementation
behavior. Review and approve it before a detailed implementation plan is written. The plan has
its own review gate before code, dependency installation, deployment, or paid execution. Existing
authorization is retained; the fifteen foundation questions are not reopened.

Reference protocol: [OpenTelemetry OTLP specification](https://opentelemetry.io/docs/specs/otlp/).
Provider pricing must be checked at implementation time against
[Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing).

# Task 4 — budgeted Jev scoring attempts

Implemented against base `3f095d3` in the approved Phase 3 worktree. All provider
executions in tests are fabricated HTTP transport responses. No provider key,
root.env, live account endpoint or paid inference was read/executed. Original
baseline and measured Task 3 containers/volumes remained stopped and untouched.

## RED / GREEN evidence

Every command below ran with explicit workdir
`/Users/bohdanburukhin/Projects/personal/touchstone/.worktrees/phase-3-reckoner-v1`
and `UV_PYTHON_INSTALL_DIR=/private/tmp/touchstone-uv-python`,
`UV_CACHE_DIR=/private/tmp/touchstone-uv-cache`.

1. Initial RED:
   `uv run --all-packages pytest workloads/reckoner/tests/test_v1_jev.py -q`
   — **19 failed, 2 passed**. Missing provider module, exact-sum validator rejected
   approved tolerance boundaries, and cross-tenant bare transaction references
   collapsed identical opaque IDs.
2. Initial budget RED:
   `uv run --all-packages pytest workloads/reckoner/tests/test_v1_budget.py -q --tb=short`
   — **10 failed** after disposable PostgreSQL setup; missing v1 accounting modules.
   An earlier sandboxed invocation produced **10 setup errors** because local TCP
   was denied. Narrow escalation then ran the actual RED tests successfully.
3. Provider/shared-contract GREEN:
   `uv run --all-packages pytest workloads/reckoner/tests/test_v1_jev.py contracts/tests/test_reckoner_v1.py -q --tb=short`
   — **46 passed**.
4. Accounting first GREEN:
   `uv run --all-packages pytest workloads/reckoner/tests/test_v1_budget.py -q --tb=short`
   — **10 passed**. Before GREEN, **6 failed / 4 passed** exposed fabricated
   evidence timestamps not matching their imported canonical tasks; test fixtures
   were corrected rather than weakening the cutoff guard.
5. Expanded circuit/CLI/pricing run:
   same budget command — **14 passed / 2 failed**. CLI attempted to load an absent
   env file before checking exact approved cases: a concrete RED for missing case
   validation. The pricing fixture used schema-invalid `.043`; corrected `0.043`
   reaches the actual unknown-pricing guard.
6. Focused new and original regressions:
   `uv run --all-packages pytest workloads/reckoner/tests/test_v1_budget.py workloads/reckoner/tests/test_v1_jev.py workloads/reckoner/tests/test_budget.py workloads/reckoner/tests/test_provider.py workloads/reckoner/tests/test_v1_storage.py contracts/tests/test_reckoner_v1.py workloads/reckoner/tests/test_v1_features.py workloads/reckoner/tests/test_v1_indicators.py -q --tb=short`
   — **124 passed in 75.75s**.
7. Self-review narrow RED:
   `uv run --all-packages pytest workloads/reckoner/tests/test_v1_jev.py workloads/reckoner/tests/test_v1_budget.py -k 'nan_http or http_decimal or expired_circuit' -q --tb=short`
   — **2 failed, 1 passed, 37 deselected**. Standard float JSON parsing rounded an
   actually outside-tolerance probability into validity; NaN protected body failed
   JSONB persistence. Decimal-text JSON decoding and nonfinite rejection correct
   both. The concurrent half-open/rate test passed.
8. Final affected-task GREEN:
   `uv run --all-packages pytest workloads/reckoner/tests/test_v1_jev.py workloads/reckoner/tests/test_v1_budget.py -q --tb=short`
   — **40 passed in 30.50s**.
9. Final lint:
   `uv run ruff check workloads/reckoner/src/reckoner/v1/providers workloads/reckoner/src/reckoner/v1/storage/attempts.py workloads/reckoner/src/reckoner/v1/storage/budget.py workloads/reckoner/src/reckoner/v1/contracts.py workloads/reckoner/src/reckoner/v1/cli.py workloads/reckoner/src/reckoner/v1/evidence/assemble.py workloads/reckoner/tests/test_v1_jev.py workloads/reckoner/tests/test_v1_budget.py contracts/tests/test_reckoner_v1.py`
   — **All checks passed!** `git diff --check` also exited zero.

Integration commands used
`RECKONER_TEST_OWNER_DSN=postgresql://postgres:fabricated-task4-local@127.0.0.1:55436/postgres`.
These are fabricated test credentials. The dedicated
`touchstone-phase3-task4-pg` container uses the approved pgvector 0.8.6 / Postgres17
image digest, 384 MiB memory, a unique loopback port and no mounted preserved
volume. The existing fixture creates/removes a distinct database and login roles
per test. Narrow escalations permitted this local container/TCP access only.
No broad baseline suite, archive preparation, or measured-source regeneration ran.

## Accounting, concurrency and restart results

- Two simultaneous final-cent protocol reservations cannot both succeed.
- Open protocol capacity is counted once globally, with attempts allocated within
  it; retries receive distinct immutable call IDs and maxima. Closing a protocol
  releases unused envelope only; uncertain calls retain their maximum liability.
- Repeated identical reservations/settlements return existing state. Conflicting
  known settlements fail; uncertainty can acquire a later immutable known event.
- Actual cost above an attempt reservation blocks further provider dispatch;
  token usage over either bound also blocks. Typesafe and Anthropic totals are
  separate. At 2,000 Jev input tokens cost is exactly `Decimal('0.000084')`.
- An empty Anthropic ledger is rejected. Owner verification checks actual existing
  1,040 settled entries, USD .493151 and their content hash under advisory lock
  `732019102`; provenance is append-only and tenant tagged. The accounting query
  includes those legacy entries exactly once. Tests seed explicitly fabricated
  legacy entries with this aggregate; implementation never synthesizes ledger
  calls or subtracts a hardcoded balance. Actual original-ledger restore/verify
  remains an unexecuted measurement gate.
- Response + score + settlement persist atomically before workflow checkpoints.
  Repeating score_task, including through a new repository connection, sends only
  one HTTP request. A reservation without persisted response becomes uncertain;
  read timeout never automatically repeats a potentially billed call.
- Five transient/connect failures open a 60-second persisted circuit. Two workers
  racing an expired circuit produce one probe reservation; provider active-call
  state enforces sequential dispatch. Rate deadlines survive restart. A 61-second
  Retry-After defers without a second attempt. 401/422 are not retried; 429/529
  retry at most three times with persisted deadlines and request-seeded jitter.

## Changed files

- New `v1/providers/__init__.py`, `v1/providers/jev.py`.
- New `v1/storage/budget.py`, `v1/storage/attempts.py`.
- New migration `009_v1_provider_budget.sql`; existing applied migrations unchanged.
- New `prompts/jev-choice-v1.json`, `config/jev-prices-v1.json`.
- New `tests/test_v1_jev.py`, `tests/test_v1_budget.py`.
- Narrow edits to `v1/cli.py`, shared `v1/contracts.py`,
  `v1/evidence/assemble.py`, original shared schema test and compatibility notes.

Self-review checked tenant-qualified protocol/call/response/settlement foreign
keys, protected body permissions, immutable records, exact decimal tolerance,
request/secret separation, cutoff identity, replay behavior, and accounting
hierarchy. V0 Anthropic request code and legacy accounting implementation remain
unchanged. Shared neighbourhood refs now hash `[tenant_id,"transaction",id]`, the
same identity as display transaction nodes; no separator concatenation and no
source identity changes.

## Downstream interfaces and remaining gates

- `JevClient(api_key: str, *, transport=None).evaluate(request: dict) -> dict`:
  single HTTP attempt, 5s connect / 30s response, explicit key, pinned binary
  question. Fractional JSON values retain exact decimal text.
- `score_task(repo, client, task, evidence, protocol) -> dict`: strict raw score
  record, or `{scorer_status: "unavailable", degraded_reason: "..."}` for circuit,
  outstanding-dispatch or rate deferral. A skip has no fake call or probability.
  `BudgetExceeded` propagates and leaves unexecuted tasks incomplete.
- `ProviderBudget(connection).reserve(call, maximum, protocol)`;
  `settle(call_id, usage, cost)`; `close(protocol_id)`; owner `verify_legacy()`.
  Protected response records are keyed by immutable call_id and reused before any
  dispatch. Task 5 owns applying calibration; Task 6 owns workflow checkpoints.
- CLI preflights all manifest cases and pinned prepared evidence before dispatch.
  Exact protocol vocabulary and runner-only environment are documented in
  `docs/operations/reckoner-v1-compatibility.md`. Protocol request hashes are hashes
  of `build_request(canonical_transaction, strict_evidence)`; Task13 must review
  exact cases, requests, bounds, purpose and cap before separately authorized runs.
- Provider metadata rechecked read-only at
  https://docs.typesafe.ai/api and https://docs.typesafe.ai/models. Actual key
  access, credits, account rate limits, paid execution and original ledger import
  were not verified here.
- The documented Jev endpoint exposes no output-token limit parameter. Local
  input preflight uses complete-request UTF-8 bytes as a conservative textual
  token bound; actual usage is reconciled and overages block dispatch. Live
  provider overhead/token accounting still needs the separately approved pilot.
- Uncertain delivery remains conservatively non-retriable through score_task;
  explicit cost reconciliation can append settlement, but recovering a missing
  provider response/redrive requires a separately reviewed operational action.
- Full frozen source/vector population and graph coverage remain Task11/13 gates.
  No synthetic HTTP results in these tests constitute calibrated or live results.

Independent review is required before dependent implementation proceeds.

## Review fix round 1 — base 6632f12

Addressed only the two Important review findings. Failed half-open probes
(timeout, invalid response, 401, 422, 500) retain the failure threshold and reopen
for 60 seconds; a valid response closes the circuit. Nonfinite Retry-After values
are invalid hints and use bounded normal backoff. Excessive finite waits persist
as deferral at the maximum representable UTC deadline, preserving response and
settlement and releasing the active dispatch instead of raising OverflowError.

Exact focused commands, with the same workdir/UV variables and disposable
Postgres DSN documented above:

- RED: `uv run --all-packages pytest workloads/reckoner/tests/test_v1_jev.py -k nonfinite_retry_after -q --tb=short` — **4 failed, 22 deselected**.
- RED: `uv run --all-packages pytest workloads/reckoner/tests/test_v1_budget.py -k 'only_successful_half_open or excessive_retry_after' -q --tb=short` — **7 failed, 1 passed, 18 deselected**. Both reviewed failures reproduced directly: failed probes cleared the circuit; Infinity/1e300 hints raised inside response persistence.
- GREEN: `uv run --all-packages pytest workloads/reckoner/tests/test_v1_jev.py -k 'retry_after or error_response' -q --tb=short` — **8 passed, 18 deselected**.
- GREEN: `uv run --all-packages pytest workloads/reckoner/tests/test_v1_budget.py -k 'only_successful_half_open or excessive_retry_after or five_transient or expired_circuit or retry_after_over_60' -q --tb=short` — **11 passed, 15 deselected in 19.65s**.
- `uv run ruff check workloads/reckoner/src/reckoner/v1/providers/jev.py workloads/reckoner/src/reckoner/v1/storage/attempts.py workloads/reckoner/tests/test_v1_jev.py workloads/reckoner/tests/test_v1_budget.py` — **All checks passed!** `git diff --check` exited zero.

Only the two implementation files and their two existing test files changed.
No broad regression rerun, provider-key read, paid inference, or preserved-store
changes occurred. The existing disposable Task 4 container alone was restarted
for the transactional checks. Task13 approval-record work remains out of scope.

# Phase 1 — Reckoner v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. The human already selected fresh subagents and reviews; do not ask them to select execution methodology again.

**Goal:** Deliver a reproducible 1,000-case simulated transaction baseline with durable decisions, bounded provider spending, independently evaluated metrics, and replayable OTLP evidence.

**Architecture:** A sequential CLI runner shares Reckoner's package/image with FastAPI and persists operational state in Postgres. An oracle-isolated provider adapter makes one Anthropic call per task through LiteLLM; a separate evaluator produces generic outcomes and reports. Durable OTLP exports preserve the evidence for Phase 2 without introducing its collector/warehouse stack.

**Tech Stack:** Python 3.12, uv, FastAPI, JSON Schema, psycopg 3, PostgreSQL 17, LiteLLM SDK, Anthropic token-counting API, OpenTelemetry Python SDK/protobuf, pytest, Ruff, Compose, kind.

**Spec:** `docs/spec/001-reckoner-v0.md` (approved); `docs/spec/000-foundation.md` supplies unchanged contracts and metric definitions.

**Plan status:** Proposed for review. No implementation, dependency installation, deployment, or provider request has begun under this plan.

## Global Constraints

- All transaction data is simulated. No production or real-customer claim is permitted.
- One repository: `platform/`, `workloads/reckoner/`, `web/`, `contracts/`, `infra/`, `docs/`.
- Touchstone ingests only through OTLP; no Reckoner imports or operational-table reads by the platform.
- Every application row carries `tenant_id`; use tenant-inclusive keys, joins, and references.
- Python `>=3.12,<3.13`; uv workspace and committed lockfile; Ruff; Conventional Commits.
- USD and UTC are explicitly recorded experimental assumptions, not verified source facts.
- Preserve every source fraud row and complete selected histories; source archive remains unchanged.
- Seed `20260925`; baseline contains exactly 100 fraud and 900 legitimate purchases from 2019.
- A separate pre-2019 pilot contains 2 fraud and 18 legitimate purchases.
- Haiku 4.5 initially; file-based immutable run settings now; shared Admin UI later without schedule changes.
- Temperature 0; maximum 256 output tokens; timeout 60 seconds; one in-flight call; no automatic retries, caching, tools, repair calls, or provider fallback.
- V0 provider failures remain failed/incomplete; no fabricated escalation, no dropped denominator cases.
- Initial global provider budget $10; pilot sub-limit $1; both include prior runs and both tenants.
- Review cost $4; false-decline loss is amount × 0.30; missed-fraud loss is the full amount.
- Current resource envelope: 8 GB RAM, 4 GB swap, approximately 80 GB free disk; measure before execution.
- Run Compose and kind separately; preserve paid-run state and artifacts during cleanup.
- No Jev, graph, retrieval, embeddings, frontend, production collector, ClickHouse, or warehouse implementation here.
- Fresh implementation subagent per task; TDD, self-review, task review, whole-branch review, finish-branch.

## Review Focus

1. **Crash after dispatch:** no repeat provider call; reservation survives; run is visibly uncertain. Tests: Tasks 2 and 4.
2. **Two tenants/runners racing the budget:** one shared cap and exclusive dispatch; no per-tenant budget reset. Tests: Task 2.
3. **Leakage through diagnostics/artifacts:** runtime role cannot read oracle; prompt allowlist and HTTP errors cannot disclose labels or credentials. Tests: Tasks 2, 3, and 5.
4. **Incomplete but superficially plausible metrics:** failed cases remain denominators; missing usage is not free; duplicate telemetry does not inflate cost. Tests: Tasks 4 and 5.
5. **Source or configuration changes during a resumable run:** immutable identity checks reject changed bytes/settings; repeated import/export is idempotent. Tests: Tasks 1, 2, and 4.

## File ownership and dependency order

Paths below are relative to the repository root. `R` in prose means
`workloads/reckoner/src/reckoner`; every task supplies full paths in its file list.

| Task | Responsibility | Dependencies |
|---|---|---|
| 1 | Adapter, selection, history/tenant manifests, artifact verifier | Existing contracts/profiler |
| 2 | Run configuration, Postgres repositories/roles, durable spend state | Task 1 artifacts |
| 3 | Prompt, provider boundary, strict output, usage/pricing | Task 2 configuration |
| 4 | Resumable runner, OTel instrumentation, durable OTLP export | Tasks 2–3 |
| 5 | Oracle evaluation, reports, read-only API | Tasks 1–4 |
| 6 | Deployment/CI, source extraction, pilot/full run, evidence | Tasks 1–5 |

Do not create generic repository factories, a job queue, LiteLLM proxy, or speculative service
interfaces. Use one concrete Postgres repository and one provider adapter; dependency injection
exists only where tests need to intercept IO. Keep SQL visible and parameterized.

## Preparation after plan approval

- [ ] Use `superpowers:using-git-worktrees` to create `.worktrees/phase-1-reckoner-v0`, branch `feat/phase-1-reckoner-v0`, from the approved documentation commit. Preserve `.idea/` and other user changes.
- [ ] Read both specs and this plan in the worktree. Run the existing 58-test foundation baseline, Ruff, and `git diff --check` from a frozen uv environment before changes.
- [ ] Locate the original `archive/` and `.env` by explicit absolute paths; do not copy the credential file into a worktree, artifact, image, or git. Future CLI `--env-file` reads only supported keys without printing values.
- [ ] Record free disk, Docker limits/architecture, tool versions, and existing containers/volumes. Use distinct test namespaces/projects and a separate persistent paid-run database.
- [ ] Resolve compatible dependency releases during their owning tasks, inspect required APIs, and commit `uv.lock`. Record installed versions in the run evidence. No unpinned dependency resolution during measured runs.

## Shared document and command contracts

Add strict schemas with `additionalProperties: false` for new interchange documents. All money
is an integer number of minor units or a nonnegative decimal string; Python calculations use
`Decimal`, Postgres uses `numeric`, and JSON never serializes binary floating-point costs.

Configuration document fields: `schema_version`, `config_id`, `provider`, `model`,
`temperature`, `max_output_tokens`, `timeout_seconds`, `input_token_ceiling`, `prompt_version`,
`price_table_version`, `threshold_config_id`. `config_id` hashes every field except itself using
the existing `content_id()`. Initial model is `anthropic/claude-haiku-4-5-20251001`; provider is
`anthropic`; input reservation ceiling is 8192. Validate these against capability/price records;
configuration cannot activate another provider implicitly.

Price document fields: `schema_version`, `price_table_version`, `model`, `currency`,
`input_per_million`, `output_per_million`, `retrieved_at`, `source_url`. Hash all but its identity.
Initial Haiku rates are `1.00` and `5.00` USD per million; reverify the official page before paid
execution. A changed rate creates a new config and invalidates the previous pilot gate.

Oracle row fields: `schema_version`, `tenant_id`, `transaction_id`, `label`, `oracle_version`,
`source_file_sha256`, `source_record`. Labels are exactly `legitimate` or `fraud`.

Decision response schema: object with required `outcome`, enum `auto-approve`, `auto-decline`,
`escalate`, and no extra fields. Omit the optional rationale to minimize output and ambiguity.
Persisted decisions additionally reference tenant, task/run/cohort/config/prompt, requested and
reported model, threshold configuration, attempt, timestamps, and code revision. Score, effective
thresholds, scorer, and graph references are explicitly null for v0.

CLI entry point: `uv run --package reckoner reckoner ...` implemented in `reckoner.cli:main`.
Commands return 0 on complete success, 2 on invalid configuration/input, 3 on incomplete or blocked
execution. Errors contain safe category/identity only, never DSNs, credentials, raw source rows,
provider response bodies, or oracle labels.

| Command | Required inputs and effect |
|---|---|
| `prepare` | `--source-dir`, `--output`; stream and freeze pilot/baseline artifacts |
| `verify` | `--artifact-dir`, `--source-dir`; verify hashes/manifests and cohort invariants |
| `migrate` | `--env-file`; apply checked migrations as owner, no provider call |
| `import` | `--env-file`, `--artifact-dir`; idempotent owner-only transaction/oracle import |
| `preflight` | `--env-file`, `--artifact-dir`, `--config`, `--purpose pilot\|baseline`, `--run-id`; freeze/validate the run and count tokens, no generation |
| `run` | same inputs plus `--allow-paid`; reserve then execute pending tasks |
| `evaluate` | `--env-file`, `--run-id`; evaluator-role oracle joins, no provider call |
| `export` | `--env-file`, `--run-id`, `--output`; write durable OTLP bytes and manifest |
| `report` | `--env-file`, `--run-id`, `--output`; deterministic JSON/Markdown evidence |
| `replay` | `--artifact-dir`, `--endpoint`; send exported protobuf through OTLP/HTTP |
| `smoke` | `--env-file`, `--output`; fabricated 4-case local-provider run only, no Anthropic access |

Secrets supported by `--env-file`: `ANTHROPIC_API_KEY`, `RECKONER_OWNER_DSN`,
`RECKONER_RUNNER_DSN`, `RECKONER_EVALUATOR_DSN`, `RECKONER_API_DSN`. Each command constructs
only the connection it needs; the runner never opens owner/evaluator connections. Container
services receive only their own DSN; the API never receives the provider key.

## Task 1 — Canonical preparation and immutable cohort artifacts

**Files:**
- Create: `workloads/reckoner/src/reckoner/data/adapter.py`, `data/cohort.py`, `data/artifacts.py`.
- Create: `workloads/reckoner/src/reckoner/cli.py`.
- Create: `contracts/schemas/oracle-v1.schema.json`, `contracts/schemas/dataset-bundle-v1.schema.json`.
- Create: `workloads/reckoner/tests/test_adapter.py`, `test_cohort.py`, `test_artifacts.py`.
- Modify: `workloads/reckoner/pyproject.toml`, `uv.lock`, `docs/data/cohort-recipe.md`, `docs/data/source-readiness.md`.

**Interfaces:** `adapt_row(row: dict[str, str], *, source_sha256: str, source_record: int,
tenant_id: str) -> dict` returns `transaction`, `oracle`, `status`, `reason`; status is
`eligible`, `unsupported`, or `invalid`. Oracle may be null only for an invalid label.
`prepare(source_dir: Path, output: Path) -> dict` writes a bundle and returns its index.
`verify_bundle(artifact_dir: Path, source_dir: Path | None = None) -> dict` returns its verified
index or raises `ValueError`. `load_runtime(artifact_dir: Path, purpose: str) -> list[dict]`
returns only canonical transaction documents. No provider or database dependencies.

- [ ] **Step 1: Write adapter identity/money/leakage tests.** Use a local fixture helper:

```python
def source_row(amount="$20.01", label="No"):
    return {"User": "0", "Card": "0", "Year": "2019", "Month": "1",
            "Day": "2", "Time": "12:30", "Amount": amount,
            "Use Chip": "Swipe Transaction", "Merchant Name": "123",
            "Merchant City": "X", "Merchant State": "Y", "Zip": "00123",
            "MCC": "1234", "Errors?": "", "Is Fraud?": label}

def test_adapter_separates_oracle_and_keeps_duplicate_rows_distinct():
    a = adapt_row(source_row(), source_sha256="a" * 64, source_record=1, tenant_id="a")
    b = adapt_row(source_row(), source_sha256="a" * 64, source_record=2, tenant_id="a")
    assert a["transaction"]["amount_minor"] == 2001
    assert a["transaction"]["occurred_at"] == "2019-01-02T12:30:00Z"
    assert a["transaction"]["currency"] == "USD"
    assert a["transaction"]["transaction_id"] != b["transaction"]["transaction_id"]
    assert "label" not in a["transaction"]
    assert a["oracle"]["label"] == "legitimate"
```

Also parameterize `$0.00`, `$-1.00`, `$1,234.56`, fractional cents, NaN/infinity, invalid
dates/IDs/labels, missing columns, UTF-8 failures, and quoted CSV records with embedded newlines.
Unknown channels map to `other`; blank channels to `unknown`. Missing optional values remain
null; postal codes remain strings. Validate every emitted transaction/oracle against its schema.

- [ ] **Step 2: Run the focused tests red.** `uv run --package reckoner pytest workloads/reckoner/tests/test_adapter.py -q`; expect missing module/functions before implementation.
- [ ] **Step 3: Implement the minimal adapter and stable IDs.** Core identity rule:

```python
def transaction_id(source_sha256: str, source_record: int) -> str:
    return content_id({"dataset": "cctd", "source_sha256": source_sha256,
                       "source_record": source_record})
```

Use CSV data-record ordinals, not physical lines. Map the existing schema fields explicitly;
never serialize the entire source dictionary. Add normalization strings `currency_assumed=USD`
and `source_timezone_assumed=UTC`. Return a reason for unsupported/invalid records without dropping
their source accounting. Keep labels entirely outside the transaction document.

- [ ] **Step 4: Add red selection/artifact tests.** Build a tiny CSV fixture with at least
100 fraud/900 legitimate positive 2019 cases, 2/18 pre-2019 cases, a 2020 row, a negative fraud
row, and a user with no pre-holdout history. Generate it in the test using the fixture helper;
do not commit copied dataset rows. Run `prepare()` twice into different output directories:

```python
first = prepare(source_dir, tmp_path / "one")
second = prepare(source_dir, tmp_path / "two")
assert first["bundle_id"] == second["bundle_id"]
baseline = load_runtime(tmp_path / "one", "baseline")
assert len(baseline) == 1000
assert len({row["transaction_id"] for row in baseline}) == 1000
assert all(row["occurred_at"].startswith("2019-") for row in baseline)
assert first["history"]["covered_fraud"] == first["history"]["source_fraud"]
```

Check 100/900 oracle counts, pilot disjointness, complete user ownership, changed source-byte
rejection, tampered runtime/oracle/manifest rejection, and insufficient-cohort failure with no
published partial bundle. Changing holdout labels must not change tenant assignment for users
with identical pre-holdout records. A user's future fraud may affect retention, never runtime inputs.

- [ ] **Step 5: Implement bounded-memory passes and atomic publication.** First pass hashes and
validates sources, accumulates per-user pre-holdout counts/risk and all-fraud user IDs, and checks
card reference coverage. Tenant B is the sorted prefix nearest 30% pre-holdout volume (tie: smaller
prefix); compare fractions exactly with `Fraction`, not float. Zero-history users use seeded hash
modulo 10,000, assigning values below 3,000 to B. Select cases with bounded heaps ordered by
`(sha256(seed, purpose, transaction_id), transaction_id)` per class; pilot uses all eligible pre-2019
records. A final streaming pass calculates exact complete-history/entity/count coverage for the
union of fraud users and selected pilot/cohort users. Recheck source hashes before publication.

The bundle index names/checksums all runtime, oracle, tenant, history, normalization, and cohort
files. Each tenant-owned record has `tenant_id`; the top-level index is artifact metadata.
Deterministic identities exclude wall-clock build timestamps. Histories reference the original
archive plus complete user IDs and record counts. Set graph coverage to none/zero. Distinguish
source counts, retained counts, eligible counts in the relevant interval, and mutually exclusive
invalid/unsupported counts. Write into a sibling staging directory then atomically rename;
refuse overwriting a different existing bundle. Expose `prepare` and `verify` commands.

- [ ] **Step 6: Run green tests and commit.**

```bash
uv run --package reckoner pytest workloads/reckoner/tests/test_adapter.py workloads/reckoner/tests/test_cohort.py workloads/reckoner/tests/test_artifacts.py -q
git diff --check
```

Update the recipe with approved USD/UTC and source-backing behavior. Commit only this task's
files as `feat: prepare reproducible baseline cohorts`; obtain independent task review.

## Task 2 — Postgres run state, configuration, and budget ledger

**Files:**
- Create: `workloads/reckoner/src/reckoner/baseline/__init__.py`, `baseline/config.py`.
- Create: `workloads/reckoner/src/reckoner/storage/__init__.py`, `storage/migrate.py`, `storage/postgres.py`, `storage/budget.py`, `storage/migrations/001_baseline.sql`.
- Create: `contracts/schemas/run-config-v1.schema.json`, `contracts/schemas/price-table-v1.schema.json`, `contracts/schemas/decision-v1.schema.json`.
- Create: `workloads/reckoner/config/baseline-v1.json`, `workloads/reckoner/config/anthropic-prices-v1.json`.
- Create: `workloads/reckoner/tests/test_config.py`, `test_storage.py`, `test_budget.py`, `conftest.py`.
- Create: `infra/compose.test.yaml` (isolated Postgres integration fixture), `.env.example` (empty keys only).
- Modify: `workloads/reckoner/src/reckoner/cli.py`, `workloads/reckoner/pyproject.toml`, `pyproject.toml`, `uv.lock`.

**Interfaces:** `load_config(path: Path, price_path: Path) -> dict` returns validated immutable
content. `migrate(owner_dsn: str) -> None` applies checksum-verified numbered SQL migrations.
`PostgresRepository(dsn: str)` is a context manager with `import_bundle(bundle: Path) -> None`,
`create_run(run_id: str, purpose: str, config: dict, bundle_id: str) -> None`,
`pending_tasks(run_id: str) -> list[dict]`, `snapshot(run_id: str, tenant_id: str | None) -> dict`,
and `exclusive_runner() -> ContextManager[None]`. Task documents contain `tenant_id`, `run_id`,
`task_id`, `transaction`, `status`. Create all expected tasks before any generation.

`BudgetLedger(repo)` supplies `reserve(task: dict, maximum: Decimal) -> dict`,
`settle(call_id: str, actual: Decimal | None, usage: dict | None) -> None`,
`remaining() -> Decimal`, and `pilot_remaining() -> Decimal`. Reservation documents contain
`tenant_id`, `run_id`, `task_id`, `call_id`, `maximum`, `status`.

- [ ] **Step 1: Write red config and migration tests.** Config tampering, unknown properties,
unknown/unpriced models, nonzero retries, invalid decimal values, and changed prompt/price IDs
must be rejected. Integration tests create a unique test database/roles through an owner DSN;
mark them `integration` and fail a required integration job if its DSN is absent. Add
`psycopg[binary]>=3,<4` only when implementing this task; pin the resolved version in the lockfile.
Verify migration rerun is idempotent and edited already-applied migration text is rejected.

- [ ] **Step 2: Run tests red, then implement tables and role grants.** Use immutable JSONB
documents plus typed identity/status/money columns where constraints and accounting require them.
Tables: `transactions`, `cohorts`, `cohort_members`, `run_configs`, `runs`, `tasks`, `attempts`,
`decisions`, `oracle_labels`, `evaluations`, `telemetry_outbox`, `budget_entries`. All contain
`tenant_id NOT NULL`; logical `run_id` groups tenant run rows. Oracle is in schema `oracle`,
operational tables in `reckoner`. Composite foreign keys include tenant, run/task where applicable.

```sql
CREATE TABLE reckoner.tasks (
  tenant_id text NOT NULL,
  run_id text NOT NULL,
  task_id text NOT NULL,
  transaction_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('pending','dispatched','completed','failed','uncertain')),
  PRIMARY KEY (tenant_id, run_id, task_id),
  UNIQUE (tenant_id, run_id, transaction_id)
);
REVOKE ALL ON SCHEMA oracle FROM PUBLIC;
```

Complete FK definitions after their referenced tables are created. Owner imports/migrates;
runner reads runtime inputs and writes attempts/decisions/outbox; evaluator reads oracle and
results and writes evaluations/outbox; API reads sanitized operational/report views only.
Runner cannot read evaluations exposing correctness either. No public oracle grants or owner
DSN fallback. Verify privileges by connecting as each real role, not by mocking SQL checks.
The paid pilot gate depends only on response validity, usage, bounds, config identity, and
export completion; expose those non-oracle fields to the runner. It does not depend on fraud
labels, correctness, or attaining a favorable quality score.

- [ ] **Step 3: Add race/recovery tests red.** The `pg` pytest fixture creates a fresh test
database, applies migrations, imports Task 1 fabricated artifacts, and exposes per-role DSNs.
Never point it at the persistent measured database. `seed_run(pg, run_id, tenant_id)` creates a
one-task fabricated run using valid configs; define that helper in `conftest.py`.

```python
def test_budget_is_shared_and_uncertain_cost_stays_reserved(pg):
    a = seed_run(pg, "r1", "a")
    b = seed_run(pg, "r2", "b")
    with PostgresRepository(pg.runner_dsn) as repo:
        ledger = BudgetLedger(repo)
        reserved = ledger.reserve(a, Decimal("6.00"))
        ledger.settle(reserved["call_id"], None, None)
        assert ledger.remaining() == Decimal("4.00")
        with pytest.raises(BudgetExceeded):
            ledger.reserve(b, Decimal("4.01"))
```

Define `BudgetExceeded` and `RunBusy` in `storage/budget.py`. Add a two-connection barrier test:
two $6 reservations race, exactly one succeeds. Test restarted repository sees identical spend;
pilot aggregate cannot exceed $1 even over new run IDs; repeated settlement is idempotent;
conflicting second settlement is rejected; cross-tenant FK misuse fails. Crash after reservation
must leave a dispatched/uncertain record and hold funds. Unrelated schema-version bookkeeping
is exempt from tenant data, not a loophole for application rows.

- [ ] **Step 4: Implement serialized accounting and immutability.** Use a fixed Postgres
session advisory lock for the active runner; a separate transaction-scoped advisory lock covers
all reserve/settle operations. Under the accounting lock, compute global encumbrance as settled
actual costs plus outstanding/uncertain maximum reservations. Sum across both tenants and all
run IDs in this database; no delete/reset API. Pilot spend is similarly summed by purpose.
Commit reservation and `dispatched` state before returning its call ID. If actual exceeds
reservation, persist the actual overage and block future dispatch; never hide the charge.

Use parameterized psycopg SQL and explicit transactions. Immutable imports/config/decisions use
insert-or-compare semantics: same bytes/identity is a no-op, a mismatch raises. Store request and
response allowlisted documents plus hashes durably with attempts. Do not log arbitrary exceptions
whose strings may include DSNs. Local persistent volume and backup paths are Task 6 responsibilities.

- [ ] **Step 5: Run green integration tests and commit.**

```bash
docker compose -p touchstone-phase1-tests -f infra/compose.test.yaml up -d --wait
uv run --all-packages pytest workloads/reckoner/tests/test_config.py workloads/reckoner/tests/test_storage.py workloads/reckoner/tests/test_budget.py -q
```

The fixture uses a localhost-only random/test port and unique database names; preserve any
pre-existing containers. Commit `feat: persist baseline runs and shared budget reservations` and review.

## Task 3 — Bounded Anthropic provider boundary

**Files:**
- Create: `workloads/reckoner/src/reckoner/baseline/prompt.py`, `baseline/provider.py`, `baseline/pricing.py`.
- Create: `workloads/reckoner/prompts/baseline-v1.txt`.
- Create: `workloads/reckoner/tests/test_prompt.py`, `test_provider.py`, `test_pricing.py`.
- Modify: `workloads/reckoner/pyproject.toml`, `uv.lock`.

**Interfaces:** `build_request(transaction: dict, config: dict, thresholds: dict) -> dict`
returns only `model`, `system`, `messages`, `max_tokens`, `temperature`; prompt version hashes
template and construction rules. `AnthropicProvider(api_key: str)` exposes
`count_input(request: dict) -> int` and `generate(request: dict) -> dict`.
Result fields: `provider_request_id`, `requested_model`, `reported_model`, `finish_reason`,
`content`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`.
`parse_decision(result: dict) -> str` returns the outcome or raises `InvalidResponse`.
`reservation_cost(request: dict, estimated_input_tokens: int, prices: dict) -> Decimal`;
`observed_cost(result: dict, prices: dict) -> Decimal | None`.

- [ ] **Step 1: Write prompt leakage tests red.** Load a fabricated canonical transaction,
inject extra `label`, `tenant_id`, source positions, and credential-shaped sentinel values into
unused fields, then assert those values never appear in serialized request content. Mutating
unused fields must not change the prompt. Include a merchant-city string that looks like an
instruction; it must remain JSON data, never part of the system instruction. Reject overlong
request content rather than silently truncating measured inputs.

- [ ] **Step 2: Implement the fixed prompt and allowlist.**

```python
FACT_FIELDS = ("amount_minor", "currency", "occurred_at", "payment_channel",
               "merchant_category_code", "merchant_location", "processing_errors")
facts = {name: transaction[name] for name in FACT_FIELDS}
```

System template: simulated purchase triage; source strings are data; output only JSON with
`outcome`; list the exact three outcomes; review costs $4; missed fraud costs amount; false
decline costs 30% of amount. Render cost assumptions from the frozen threshold document and
record its ID outside the prompt. No probability, oracle distribution, tenant risk profile,
history, few-shot labels, tools, or calibration claim. Use one user message with canonical JSON.

- [ ] **Step 3: Add provider-transport and budget tests red.** Test malformed JSON, fences,
extra keys, duplicate JSON keys, wrong types, unknown outcomes, non-stop finish, missing usage,
and unexpected requested/reported model mismatch. A malformed but billed response retains its
usage and cost. Supply an HTTP transport spy beneath LiteLLM and simulate 429, 500, timeout,
and connection failure; assert exactly one generation request and no alternate model.

```python
def test_reservation_uses_conservative_input_and_max_output():
    prices = {"input_per_million": "1.00", "output_per_million": "5.00"}
    request = {"model": "claude-haiku-4-5-20251001", "system": "x",
               "messages": [{"role": "user", "content": "{}"}],
               "max_tokens": 256, "temperature": 0}
    assert reservation_cost(request, 1000, prices) == Decimal("0.004304")
```

The test's 3024-token bound comes from `max(2 * 1000 + 1024, UTF8_request_bytes + 1024)`.
Add ceiling breach, negative/missing counts, non-ASCII content, actual-over-reservation, and
unknown price tests. Money rounding is upward to a micro-dollar only for reservations;
observed usage is retained at exact decimal precision.

- [ ] **Step 4: Implement provider-compatible counting and a conservative reservation.**
Use Anthropic's count endpoint for the exact native system/messages payload with matching model;
configure its client with `max_retries=0`, timeout 60. Counting is not a paid generation call;
count failures block dispatch. Serialize the exact native request with compact UTF-8 JSON.
Strip only the `anthropic/` routing prefix for the native model ID; preserve both forms in
provenance. Compare the reported model with the native pinned ID, not the LiteLLM route string.

```python
bound = max(2 * estimated_input_tokens + 1024, len(native_request_bytes) + 1024)
if bound > 8192:
    raise ValueError("input reservation ceiling exceeded")
reserved = (Decimal(bound) * Decimal(prices["input_per_million"])
            + Decimal(256) * Decimal(prices["output_per_million"])) / Decimal(1_000_000)
reserved = reserved.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)
```

This is a conservative operational bound, not a provider invoice guarantee; Anthropic documents
token counts as estimates. Pin the formula/version and retain estimate/bound/actual for every
call. If the pilot exceeds a bound, stop; do not silently increase it. Up to 8192 input plus 256
output at current prices reserves $0.009472/call; 1,020 calls would reserve $9.661440 before other
spend. Full preflight sums the actual per-request reservations, not a mean-only extrapolation.

For generation use `litellm.completion` with explicit `anthropic/` route, `num_retries=0`,
`max_retries=0`, `timeout=60`, `max_tokens=256`, temperature 0, stream false. Verify the pinned
library honors these arguments with the transport tests. Request JSON through the prompt and
validate locally; do not enable an implicit tool-based JSON emulation. This avoids depending on
inconsistent Haiku structured-output documentation. One malformed response is one failed task.
Set no caching controls, tools, thinking, proxy callbacks, or remote logging. Disable LiteLLM's
remote price-map refresh; use the committed price record. Unknown/error usage returns no known
cost, never zero. Unexpected caching or model identity stops measured execution for diagnosis.

- [ ] **Step 5: Run green provider tests, then commit/review.**

```bash
uv run --package reckoner pytest workloads/reckoner/tests/test_prompt.py workloads/reckoner/tests/test_provider.py workloads/reckoner/tests/test_pricing.py -q
```

No real credentials or network are permitted in these tests. Commit `feat: add bounded Anthropic baseline calls`.

## Task 4 — Resumable runner and durable OTLP evidence

**Files:**
- Create: `workloads/reckoner/src/reckoner/baseline/runner.py`, `telemetry/__init__.py`, `telemetry/events.py`, `telemetry/otlp.py`.
- Create: `workloads/reckoner/tests/test_runner.py`, `test_telemetry.py`.
- Create: `contracts/otel-conventions.md`.
- Modify: `workloads/reckoner/src/reckoner/storage/postgres.py`, `workloads/reckoner/src/reckoner/cli.py`, `workloads/reckoner/pyproject.toml`, `uv.lock`.

**Interfaces:** `preflight(repo, provider, run_id: str) -> dict` returns per-task request hashes,
token estimates/reservations and total, without generation. `execute_run(repo, provider,
run_id: str) -> dict` returns terminal/incomplete counts. `finish_attempt(reservation: dict,
result: dict | None, error_category: str | None, timing: dict) -> None` on the repository persists
outcome, usage, state, and telemetry atomically. `export_run(repo, run_id: str, output: Path) -> dict`
writes OTLP requests plus manifest; `replay(artifact_dir: Path, endpoint: str) -> dict` sends them.

- [ ] **Step 1: Add red recovery/instrumentation tests with a fake provider.** Define a counting
fake provider implementing Task 3's two methods, returning a valid response with known usage or
raising a safe synthetic exception. It is explicitly labelled fake and never selected by missing
credentials. Reuse Task 2's real Postgres fixtures. The four-task runner fixture creates an
explicit test-mode run and an isolated test ledger; it never bypasses pilot gates for real calls.

```python
execute_run(repo, fake_provider, run_id)
calls = fake_provider.generation_calls
execute_run(repo, fake_provider, run_id)
assert fake_provider.generation_calls == calls
assert repo.snapshot(run_id, None)["completed"] == 4
```

Inject a crash immediately after durable reservation and immediately after the fake returns
but before `finish_attempt`; restarting must perform zero further generations and leave an
uncertain task/reservation. Simulate failure before reservation to prove no dispatched attempt
exists. Test insufficient budget, no successful pilot, changed request hash, changed code/config,
missing price, and export failure all leave explicit states and never cause hidden re-generation.

- [ ] **Step 2: Implement one-call state transitions.** Under `exclusive_runner`, validate all
run/config/artifact IDs and prohibit any unresolved dispatched/uncertain attempt. Preflight all
pending task requests using provider count calls; persist their hashes/estimates. For a baseline,
require a successful 20-case pilot with identical config/prompt/model/price versions and observed
usage. Confirm sum of all remaining reservations fits remaining global funds. For each task:
commit reservation → execute once → commit result/decision/failure/usage/outbox. Failed definite
responses remain failed; timeout/unknown charge stops further dispatch for reconciliation. Budget
or export failure blocks advancement. Never rerun completed/failed/uncertain tasks in the same run.

```python
with repo.exclusive_runner():
    for task in repo.pending_tasks(run_id):
        reservation = ledger.reserve(task, task["reservation_cost"])
        started_at = datetime.now(UTC)
        started_clock = time.monotonic()
        result = provider.generate(task["request"])
        timing = {"started_at": started_at.isoformat(),
                  "ended_at": datetime.now(UTC).isoformat(),
                  "duration_ms": (time.monotonic() - started_clock) * 1000}
        repo.finish_attempt(reservation, result, None, timing)
```

Implement guarded exception paths around this core: preserve usage on invalid content, classify
provider exceptions without their raw text, and leave ambiguous attempts uncertain. Timing uses
UTC timestamps plus monotonic elapsed duration; persisted start/end reflect actual dispatch.
Runner returns an incomplete result rather than fabricating outcomes. Preflight request and
reservation fields are persisted against the task and returned by `pending_tasks` after preflight,
not added to the canonical transaction. CLI preflight uses insert-or-compare `create_run` before
counting; CLI run reuses that run and its validated preflight. Missing preflight blocks generation.

- [ ] **Step 3: Add red OTLP round-trip/replay tests.** Each exported `.pb` is one serialized
`ExportTraceServiceRequest`, with a manifest of ordered filenames, SHA-256s, event IDs and run IDs.
Use generated protobuf decoding in tests. Include genuine 16-byte trace and 8-byte span IDs.
Run a local HTTP test receiver on an ephemeral port, POST protobuf to `/v1/traces`, and return an
encoded OTLP response. Confirm replay preserves bytes/IDs, duplicate exports keep identities,
HTTP errors retain pending state, partial-success rejection is not counted as complete success,
and decoded attributes contain no prompt/body/key/label sentinel.

- [ ] **Step 4: Implement durable SDK span export.** Pin semantic convention document version
`v1.37.0` and cite it in `contracts/otel-conventions.md`; it is an explicit project version, not
a claim to use the latest convention. Provider spans record `gen_ai.operation.name=chat`,
`gen_ai.provider.name=anthropic`, requested/reported model and available input/output usage.
Custom `touchstone.*` attributes hold workflow/tenant/run/task/config/cohort/event identities.
Document SDK/protobuf library versions and the JSON envelope mapping.

Use an OTel SDK exporter that serializes ended spans using the pinned OTLP protobuf encoder
and stores exact bytes in the outbox. Completion and its ended-span serialization/outbox insertion
share one database transaction; an exporter write failure rolls back completion and leaves the
prior durable dispatch uncertain. Add a test for this exact failure, not only a later disk-export
failure. Generic `measurement-v1` documents are validated before
being encoded as named span events with their JSON body in a project attribute. Outcome/eval
spans emitted later link to the original task trace. The outcome currency is USD when its costs
are known; unknown outcome costs remain null. Parent spans carry no duplicate provider cost.

Persist task/provider trace context before dispatch. A process crash leaves incomplete span
evidence; recovery may emit a linked failure event, never invent a completed provider span.
Flush SDK export synchronously before reporting success. Outbox export uses atomic file writes,
checksums and deterministic names; repeat exports reuse bytes. For replay use OTLP/HTTP protobuf
and inspect `partial_success`. Network retry of telemetry is allowed with unchanged IDs; retry
of model generation is not. A telemetry failure blocks the next measured task, preserving evidence.

- [ ] **Step 5: Run green tests and commit/review.**

```bash
uv run --all-packages pytest workloads/reckoner/tests/test_runner.py workloads/reckoner/tests/test_telemetry.py -q
```

Commit `feat: run resumable baselines with durable OTLP evidence`.

## Task 5 — Honest metrics, reports, and operational reads

**Files:**
- Create: `workloads/reckoner/src/reckoner/baseline/evaluate.py`, `baseline/report.py`, `api.py`.
- Create: `workloads/reckoner/tests/test_evaluate.py`, `test_report.py`, `test_api.py`.
- Modify: `workloads/reckoner/src/reckoner/app.py`, `storage/postgres.py`, `telemetry/events.py`, `cli.py`.

**Interfaces:** `evaluate_case(outcome: str | None, label: str, amount_minor: int,
review_cost: Decimal, margin_rate: Decimal) -> dict` returns `status`, `correct`, `review_cost`,
`error_cost`, `currency`. `evaluate_run(repo, run_id: str) -> dict` persists idempotent outcomes
and generic metric contributions as evaluator role. `build_report(snapshot: dict) -> dict` is
pure; `write_report(report: dict, output: Path) -> None` writes deterministic JSON/Markdown.
`create_app(api_dsn: str | None = None) -> FastAPI` is the app factory; retain module `app` for uvicorn.

- [ ] **Step 1: Add red known-answer tests.**

```python
@pytest.mark.parametrize("outcome,label,correct,review,error", [
    ("auto-approve", "legitimate", True, "0", "0"),
    ("auto-approve", "fraud", False, "0", "100"),
    ("auto-decline", "legitimate", False, "0", "30"),
    ("auto-decline", "fraud", True, "0", "0"),
    ("escalate", "legitimate", True, "4", "0"),
    ("escalate", "fraud", True, "4", "0"),
])
def test_cost_matrix(outcome, label, correct, review, error):
    result = evaluate_case(outcome, label, 10000, Decimal("4"), Decimal("0.30"))
    assert result["correct"] is correct
    assert Decimal(result["review_cost"]) == Decimal(review)
    assert Decimal(result["error_cost"]) == Decimal(error)
```

Use a six-case $100 fixture containing the six matrix outcomes and $0.01 model cost each:
four correct, $8 review, $130 error, $0.06 model, CPST $34.515; FP 1/3, missed fraud 1/3,
escalation 2/6, completion 6/6. Add a seventh failed legitimate case: denominator 7, legit
denominator 4, no invented error/review cost, incomplete run/full CPST unavailable. Test unknown
provider cost, no correct outcomes, no completed tasks, empty tenant population, duplicated call
IDs, and sub-cent false-decline loss. Nearest-rank p99 of [1,2,3,4] is 4, without interpolation.

- [ ] **Step 2: Implement decimal arithmetic and generic contributions.**

```python
amount = Decimal(amount_minor) / Decimal(100)
correct = outcome == "escalate" or (outcome == "auto-approve" and label == "legitimate") or (outcome == "auto-decline" and label == "fraud")
review = review_cost if outcome == "escalate" else Decimal(0)
error = amount if outcome == "auto-approve" and label == "fraud" else amount * margin_rate if outcome == "auto-decline" and label == "legitimate" else Decimal(0)
```

Handle `outcome is None` first: failed/pending outcome, null correctness/cost components, no
correct count. Compute full-run CPST only with complete outcomes and costs; publish partial
components and coverage separately rather than a misleading full CPST. Emit integer numerator/
denominator contributions for rates, decimal cost components through outcome/usage envelopes,
and evaluator version in every result. No fraud formula is added to platform code.
Schema validity and correctness are separate deterministic suites; required-suite errors cannot
produce an overall pass. Case-note, human agreement and calibration remain not evaluated.

- [ ] **Step 3: Add red API/report access tests.** Routes are `GET /health/live`,
`GET /health/ready`, `GET /tenants/{tenant_id}/runs/{run_id}`, and
`GET /tenants/{tenant_id}/runs/{run_id}/results?limit=100&offset=0`.
Readiness checks database reachability/migration version; liveness remains independent. Return
503 on unavailable readiness and sanitized DB failures, 404 for run/tenant mismatch, 422 for
limit outside 1–100 or negative offset. Parameterized SQL prevents tenant path injection.
No write route, prompt, raw response, oracle label, DSN, or key appears in any API payload/error.
Aggregate correctness metrics may be exposed after evaluator publication; individual oracle
labels stay private. Test in real-role integration plus FastAPI TestClient.

- [ ] **Step 4: Implement reports and read-only routes.** Report JSON includes schema/version,
run status, all manifest/config/prompt/model/code IDs, expected/completed/failed/uncertain counts,
per-tenant and aggregate metrics with raw numerators/denominators, exact cost components,
reservation/unknown-cost status, completed-task latency population, evaluation errors, and
trace/export references. Add mandatory simulation/enriched-sample/USD/UTC/ideal-review caveats.
No timestamps generated during report rendering may change its content hash; use stored times.
Markdown is a readable rendering of the same data, with unavailable values visibly labelled.
Expose sanitized read models through API role/views, never owner connections.

- [ ] **Step 5: Run green tests, full suite, and commit/review.**

```bash
uv run --all-packages pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
git diff --check
```

Commit `feat: report baseline quality cost and completeness`.

## Task 6 — Deployment, measured experiment, and evidence

**Files:**
- Modify: `infra/Dockerfile.reckoner`, `infra/compose.yaml`, `infra/k8s/reckoner.yaml`, `.github/workflows/ci.yaml`, `README.md`.
- Create: `infra/k8s/postgres.yaml`, `infra/k8s/migrate-job.yaml`, `infra/k8s/smoke-job.yaml`.
- Create: `docs/operations/baseline-runbook.md`, `docs/evidence/phase-1-baseline.md`.
- Modify: `docs/operations/local-runtime.md`, `docs/architecture/workspace.dsl`, `docs/architecture/foundation.md`.
- Create: `workloads/reckoner/tests/test_cli.py` (offline subprocess smoke/error coverage).
- Modify only if needed: `workloads/reckoner/pyproject.toml`, `uv.lock` for packaging resources.

**Interfaces:** CLI commands from the shared contract; the same built image serves API and CLI.
Container resources include schemas, SQL migrations, templates, and default configs. Resolve
resources using installed package paths/explicit configured paths, never the developer's cwd.

- [ ] **Step 1: Add red CLI/installed-package tests.** Build/install into an isolated test
environment; run outside the checkout. Test `--help`, invalid paths, absent credentials,
unapproved paid mode, unknown model, and incomplete execution exit code 3. `smoke` uses only
a bundled fabricated fixture and fake provider, emitting `touchstone.provider_call_mode=fake`;
it cannot pass the paid pilot gate or affect the real budget. Ensure fixture mode cannot be
selected by missing credentials. A standard `run` without `--allow-paid` makes zero requests.

- [ ] **Step 2: Extend Compose with a persistent Postgres and package the image.** Pin the
PostgreSQL 17 image to the resolved tested multi-architecture digest and record it; the exact
digest is obtained at execution, not invented here. Start with API/runner limits 512 MiB each
and Postgres 512 MiB, `shared_buffers=128MB`, bounded connections. Only runner and API needed
for the active operation run alongside Postgres; no full future stack. Postgres binds localhost
only when host CLI access is needed. Use a dedicated persistent volume/project for measured
work and a different disposable project for tests. Empty DSNs/credentials fail validation.

Migrations run explicitly as owner; database roles are provisioned from local secrets, never
committed passwords. Container API receives API-role DSN only. Mount preparation artifacts
read-only for import; do not mount archive/oracle into runner/API containers. Export artifacts
to a persistent bind mount writable by UID 10001. Docker build must exclude `.env`, `.idea`,
archive, artifacts and agent scratch; add exclusions only if current `.dockerignore` needs them.

- [ ] **Step 3: Prove Compose behavior with fabricated data before paid work.**

```bash
docker compose -p touchstone-phase1-smoke -f infra/compose.yaml config --quiet
docker compose -p touchstone-phase1-smoke -f infra/compose.yaml up -d --wait postgres
docker compose -p touchstone-phase1-smoke -f infra/compose.yaml build reckoner
```

Invoke `migrate` using the built image and owner-role env file before starting the API with
`docker compose -p touchstone-phase1-smoke -f infra/compose.yaml up -d --wait reckoner`.
Then invoke `smoke`, `evaluate`, `export`, and `report` using the built image and fabricated
run identifiers. Check readiness, expected metrics, tenant isolation, Postgres restart/resume,
and report/OTLP artifacts. Do not print `docker compose config` or inspect full environment values.
Use the project's empty example env only after locally supplying disposable test DSNs.

- [ ] **Step 4: Repeat the same image smoke in kind, separately.** Use a dedicated cluster,
kubeconfig, namespace, Postgres PVC/StatefulSet, migration Job and smoke Job. Generate local
Kubernetes Secrets from protected env files without echoing their values; no provider key is
needed for smoke. Readiness uses `/health/ready`, liveness `/health/live`. Copy generated evidence
out before deleting only the task-owned smoke cluster. Do not delete unrelated clusters or
overwrite the user's kubeconfig. Record pod limits, measured memory, restart counts and image IDs.

- [ ] **Step 5: Extend CI and verify offline software readiness.** Add a Postgres service,
role/migration setup and required integration test job. Network-intercept tests prove zero paid
provider access. CI builds the image and runs the fabricated end-to-end smoke; artifact tests
must not depend on the local CCTD archive. Pin GitHub actions as the current workflow does.
Run all test/lint/format checks locally; distinguish local workflow checks from a hosted CI run.
Get whole-branch review and resolve all dispatch/budget/leakage findings before paid generation.

- [ ] **Step 6: Prepare the real cohort and preflight.** In the implementation worktree,
set shell variables using literal local paths; never print the contents of the secret file.

```bash
reckoner_source=/Users/bohdanburukhin/Projects/personal/touchstone/archive
reckoner_env=/Users/bohdanburukhin/Projects/personal/touchstone/.env
uv run --package reckoner reckoner prepare --source-dir "$reckoner_source" --output artifacts/phase1/data
uv run --package reckoner reckoner verify --source-dir "$reckoner_source" --artifact-dir artifacts/phase1/data
uv run --package reckoner reckoner migrate --env-file "$reckoner_env"
uv run --package reckoner reckoner import --env-file "$reckoner_env" --artifact-dir artifacts/phase1/data
uv run --package reckoner reckoner preflight --env-file "$reckoner_env" --artifact-dir artifacts/phase1/data --config workloads/reckoner/config/baseline-v1.json --purpose pilot --run-id phase1-pilot-001
```

The runbook must explain how local Postgres role DSNs are populated without touching the
existing Anthropic key. Record source and artifact hashes, 29,757 fraud-history coverage,
100/900 baseline and 2/18 pilot counts, temporal boundaries, actual tenant distributions,
unsupported records, preparation time/RSS/disk. Any mismatch stops the measured experiment.
Recheck provider price/model availability and record the verified values. Approval of this
plan authorizes the bounded experiment described here; do not repeatedly request permission
for the same $10 experiment, but stop if its approved constraints cannot be satisfied.

- [ ] **Step 7: Execute the paid pilot and gate the full run.** Use unique run IDs with
persisted config/code revision, e.g. `phase1-pilot-001` and `phase1-baseline-001`.

```bash
uv run --package reckoner reckoner run --env-file "$reckoner_env" --artifact-dir artifacts/phase1/data --config workloads/reckoner/config/baseline-v1.json --purpose pilot --run-id phase1-pilot-001 --allow-paid
uv run --package reckoner reckoner evaluate --env-file "$reckoner_env" --run-id phase1-pilot-001
uv run --package reckoner reckoner export --env-file "$reckoner_env" --run-id phase1-pilot-001 --output artifacts/phase1/pilot/otlp
uv run --package reckoner reckoner report --env-file "$reckoner_env" --run-id phase1-pilot-001 --output artifacts/phase1/pilot/report
```

Report pilot spend, correctness, schema validity, latency, and full-run cost projection. A poor
but valid decision is legitimate baseline evidence, not grounds to tune on the frozen cohort.
Require 20 valid pilot responses, known usage, no reservation overage, valid export, and the
full remaining reservation sum within funds. If a pilot error blocks this gate, report it; no
automatic paid rerun. Any new approved retry uses a new run ID and retains original costs.

When the gate passes, preflight and run `--purpose baseline --run-id phase1-baseline-001` with
the same other arguments, then evaluate/export/report that run. Never reset the ledger or rerun
to obtain a more flattering result. Known failed cases stay failed; uncertain charges block
additional dispatch. An interrupted run may resume only its never-dispatched tasks after all
uncertainty is reconciled with evidence. Unknown charges remain reserved without speculation.

- [ ] **Step 8: Record evidence, back up state, and finish.** Export a database backup and
artifact checksums into ignored local storage; verify backup readability before stopping the
measured services. Stop containers without deleting their volumes. Document a restart path and
the budget ledger location so future phases cannot accidentally reset the $10 cap.
Update current/planned Structurizr and Mermaid flows and perform the required archify drift
check against actual code. Commit only aggregate evidence, diagrams, docs and product files;
never source records, model artifacts, credentials, `.idea`, or `.superpowers` scratch.

```bash
uv run --all-packages --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
git diff --check
git status --short
git ls-files archive artifacts .env .idea .superpowers
```

The final tracked-file audit compares with the baseline: an inherited Phase 0 scratch report
is not authorization to add more scratch; leave unrelated cleanup separate. Re-run review if
post-review changes affect behavior. Commit `feat: validate and measure the Reckoner baseline`
(or use `docs: record incomplete baseline experiment` when only blocked-run evidence is added).
Use `superpowers:finishing-a-development-branch`; no merge/push or Phase 2 work is implied.

## Acceptance and self-review map

| Specification requirement | Owning task and proof |
|---|---|
| USD/UTC, source/card mapping, complete histories, cohort split | 1: source fixtures, hashes, real preparation evidence in 6 |
| Configurable Haiku, immutable settings, UI deferred | 2–3: strict configs and recorded request/model IDs |
| Oracle isolation and tenant ownership | 1–2, 5: separated files, real-role/FK/API tests |
| One call, explicit failures, safe recovery | 3–4: transport-spy and crash tests |
| Global/pilot budget, exact reservation, no reset | 2–4: real database race/accounting plus pilot gates |
| Durable trace evidence and OTLP-only platform boundary | 4: decoded protobuf, HTTP receiver, replay tests |
| Honest CPST, rates, p99, incomplete data | 5: known-answer matrix and incomplete-population fixtures |
| Operational API, packaged resources, both deployment paths | 5–6: API/installed-image and Compose/kind smoke |
| Measured pilot/baseline, resource and architecture evidence | 6: real artifacts or explicit blocker, drift check |

Self-review must confirm each interface name above matches its consumers, every Review Focus
line has a named test, and no executable behavior is left to an undefined helper. Test fixture
builders described within their owning task are test code, not production abstractions. Runtime
counts, image digests, resolved library versions, and paid outputs are measured during execution;
they are not fabricated in this planning document.

## Primary references checked during planning

- [Anthropic model IDs](https://platform.claude.com/docs/en/models/overview): explicit dated Haiku ID.
- [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing): initial Haiku token rates; verify again before dispatch.
- [Anthropic token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting): preflight counts are estimates.
- [LiteLLM Anthropic provider](https://docs.litellm.ai/docs/providers/anthropic): route/parameter mapping; verify pinned-version transport behavior.
- [LiteLLM retry controls](https://docs.litellm.ai/docs/routing): library retry count and provider SDK retry count are distinct.
- [OTel GenAI conventions v1.37.0](https://github.com/open-telemetry/semantic-conventions/blob/v1.37.0/docs/gen-ai/gen-ai-spans.md): versioned span vocabulary.
- [OTLP specification](https://opentelemetry.io/docs/specs/otlp/): protobuf transport and responses.
- [Psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html): explicit transaction scopes.

## Handoff

Review this plan before implementation. Execution method remains fresh subagents with task and
whole-branch reviews. Approval covers implementation and the constrained local pilot/baseline
workflow above, subject to its data/access/budget gates. Software readiness and measured-run
completion will be reported separately. The next phase and branch integration remain separate decisions.

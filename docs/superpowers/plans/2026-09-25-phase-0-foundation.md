# Phase 0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. The user has already selected fresh-subagent execution through the project methodology. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Establish a tested, resource-measured foundation: source-data validation, versioned contracts and experiment manifests, and a minimal FastAPI service that runs independently in Docker Compose and local kind.

**Architecture:** Keep CCTD handling in `workloads/reckoner/` and language-neutral schemas in `contracts/`. Build a streaming source profiler and contract tests before adding a health-only FastAPI deployment. This phase makes no model calls and starts no database or measurement stack.

**Tech Stack:** Python 3.12, uv, pytest, Ruff, JSON Schema Draft 2020-12, FastAPI, Uvicorn, Docker Compose, kind, kubectl, GitHub Actions, Structurizr DSL.

**Spec:** `docs/spec/000-foundation.md` — approved in conversation on 2026-09-25. Also read `PROJECT_BRIEF.md` completely.

**Plan status:** Implemented and reviewed on 2026-09-25; awaiting the human’s branch integration decision.

## Global Constraints

- “Touchstone ingests workload data **only through OTLP**.” No platform import of Reckoner code.
- “dbt models use generic workflow dimensions including `workflow_id`, `node_name`, and `tenant_id`.” No fraud business logic in platform code.
- “Every persisted application/event row and graph node has `tenant_id`.” Offline corpus inventories are source provenance, not application transaction records; application fixtures must be tenant-scoped.
- “The initial frozen cohort contains exactly 1,000 unique transactions: 100 fraud and 900 legitimate.” This phase defines its manifest and construction recipe; actual cohort extraction belongs to Phase 1.
- “For each evaluated transaction, scoring, graph features, and retrieval may use only evidence available before its timestamp.”
- “Scoring | Jev; wait for access, with no stub implementation.” No alternate scorer in this phase.
- “LLM | Anthropic through LiteLLM.” No LLM calls in this phase.
- “Local resources | 8 GB RAM, 4 GB swap; use the current approximately 80 GB free disk, confirmed during planning.” Read-only inspection measured about 80.2 GiB free; the human confirmed this volume is the disk budget.
- “Local deployment | Compose and single-node kind, used separately.”
- “Budget | Provisional $5–10 monthly cap per paid service; $10 total initial LLM baseline/evaluation budget, including judges.” Phase 0 incurs no paid service usage.
- “All transaction data is simulated.” This qualification appears in reports and the project README.
- Use the approved top-level layout: `platform/`, `workloads/reckoner/`, `web/`, `contracts/`, `infra/`, `docs/`. Create files only when their task requires them; no empty services or generated frontend in Phase 0.
- Do not install or modify Codex/Claude skills or configuration. Keep agent instructions equivalent and use existing installations.
- TDD for executable behavior, an isolated worktree, a fresh implementer per task, and review before proceeding to the next task.

## Review Focus

1. Duplicate-looking transactions must remain distinct source records: Task 1 tests record counting; Task 2 tests source-position identity.
2. Money, labels, dates, and card references may be malformed: Task 1 tests explicit counters and fail-closed readiness, without dropping source rows.
3. A user index is not proof of a join to a person-name row: Task 1 records the missing explicit user key and prohibits demographic joins until verified.
4. A syntactically valid event may leak an oracle label or omit tenancy: Task 2 rejects both and tests timezone-aware timestamps.
5. A service can pass a liveness check while deployment resource settings or entrypoints are wrong: Task 4 tests the built image through both deployment paths and records actual Docker limits.

## Scope and sequence

The foundation spec spans several independently deliverable systems. Use one implementation plan per phase, rather than a single plan that attempts to build Reckoner, Touchstone, and the UI together. This plan covers Phase 0 only.

| Approved specification area | This plan / later ownership |
|---|---|
| PR/FAQ, decisions, methodology | Already in approved spec; bootstrap preserves them |
| Data semantics and resource feasibility | Tasks 1 and 4 |
| Canonical transaction and measurement contracts | Task 2 |
| Config/cohort versioning and construction definition | Task 3 |
| Compose/kind, initial diagrams, tests, linting | Tasks 1 and 4 |
| Adapter, actual cohort, Postgres, baseline and spend cap | Phase 1 plan after Phase 0 evidence |
| OTLP delivery, ClickHouse, Dagster, dbt, MetricFlow, Snowflake/DuckDB, second workflow | Phase 2 plan |
| Jev, LangGraph, graph, retrieval, routing, case notes, console/admin | Phase 3 plan, blocked on Jev access |
| Threshold sweep, budgets UI, coverage, tenant cost views | Phase 4 plan |

No future phase is considered complete through documentation alone. No application import package named `platform` is created because it would shadow Python's standard library module.

## Inspection evidence and execution preflight

Observed before writing this plan:

- Git reports an unborn `main` branch with no commits; `origin/main` is absent from local tracking state. Do not assume an empty remote or push anything.
- Existing files are untracked: `AGENTS.md`, `CLAUDE.md`, `PROJECT_BRIEF.md`, `docs/`, `archive/`, and `.DS_Store`.
- Dataset files are the main 2.2 GB transaction CSV, two dimension CSVs, and `User0_credit_card_transactions.csv`.
- Transactions expose numeric `User`/`Card` references; cards expose `User`/`CARD INDEX`; users expose `Person` names and no explicit numeric key.
- Transaction examples have `$` amounts and naive date/time fields. These do not prove a timezone or a unique currency.
- Python, uv 0.11.32, Docker CLI, kubectl, Node, npm, and git are on PATH. kind is not on PATH.
- A Docker daemon read was denied by the shell sandbox. This is not evidence that Docker is stopped or missing; recheck with appropriate execution permissions after plan approval.

No credentials or source files are copied into git. The actual RAM allocation to Docker, full source counts, and join semantics remain verification work.

## File map

| Files | Responsibility |
|---|---|
| `.gitignore`, `.dockerignore` | Keep local data, secrets, caches, and worktrees out of git/build contexts |
| `pyproject.toml`, `uv.lock`, `.python-version` | Root workspace, test/lint tools, pinned Python minor, reproducible dependencies |
| `workloads/reckoner/pyproject.toml`, `src/reckoner/__init__.py` | Installable Reckoner package |
| `workloads/reckoner/src/reckoner/data/profile.py`, `data/__init__.py` | Streaming source inventory and CLI |
| `workloads/reckoner/tests/test_profile.py` | Profiler behavior on tiny fabricated CSVs |
| `contracts/schemas/transaction-v1.schema.json` | Runtime transaction contract; no oracle |
| `contracts/schemas/measurement-v1.schema.json` | Generic pre-OTLP measurement payload |
| `contracts/examples/transaction-v1.json`, `measurement-v1.json` | Valid, clearly simulated examples |
| `contracts/tests/test_events.py` | Schema tests and source-position ID checks |
| `contracts/schemas/threshold-config-v1.schema.json`, `cohort-manifest-v1.schema.json` | Immutable configuration and cohort metadata contracts |
| `contracts/examples/threshold-config-v1.json`, `cohort-manifest-v1.json` | Versioned defaults and small fixture manifest |
| `contracts/tests/test_manifests.py` | Identity, immutability, cohort and config semantics |
| `workloads/reckoner/src/reckoner/contracts.py` | Schema loading/validation and canonical content hashing |
| `workloads/reckoner/src/reckoner/app.py`, `tests/test_app.py` | Health-only FastAPI entrypoint and test |
| `infra/Dockerfile.reckoner`, `infra/compose.yaml` | Minimal local container deployment |
| `infra/kind.yaml`, `infra/k8s/namespace.yaml`, `reckoner.yaml` | Single-node kind and namespaced API deployment/service |
| `.github/workflows/ci.yaml` | Local-contract/unit tests, lint, and build validation |
| `README.md`, `docs/architecture/workspace.dsl`, `docs/architecture/foundation.md` | Entry point and current/planned architecture views |
| `docs/data/source-readiness.md`, `docs/data/cohort-recipe.md`, `docs/operations/local-runtime.md` | Measured data facts, temporal sampling method, deployment/runbook/resource evidence |

In the table, `src/` and `tests/` after the Reckoner package refer to paths beneath `workloads/reckoner/`; Task file lists below use full paths. Generated inventories live under ignored `artifacts/`, outside the tracked documentation.

## Bootstrap — establish the first commit and worktree

This is a repository prerequisite, not a product implementation task. An unborn branch cannot provide the base commit for the required worktree. Perform these steps only after this plan is approved.

- [x] Read `superpowers:using-git-worktrees` and `superpowers:subagent-driven-development`; preserve the user's selected execution method.
- [x] Recheck `git status --short --branch`, `git log -1`, and `git ls-files`. A missing first commit is expected; changed evidence supersedes this bootstrap recipe. Never reset or discard existing work.
- [x] Check configured commit identity using `git var GIT_AUTHOR_IDENT`; do not invent a name/email or modify global git settings.
- [x] Create `.gitignore` with these entries before staging any files:

```gitignore
.DS_Store
archive/
artifacts/
.worktrees/
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.coverage
htmlcov/
.env
.env.*
!.env.example
node_modules/
.next/
```

- [x] Verify `git check-ignore archive/credit_card_transactions-ibm_v2.csv .DS_Store .worktrees/probe`. Expected: all three paths are ignored. Inspect `git status --short` before staging.
- [x] Stage only `.gitignore`, the two existing instruction files, `PROJECT_BRIEF.md`, the approved foundation spec, and this approved plan. Review `git diff --cached --stat` and `git diff --cached --name-only`. No dataset, credential, product code, or unrelated files may be staged.
- [x] Commit locally as `docs: record approved foundation specification and plan`. Do not push.
- [x] Create the implementation branch/worktree using the skill, suggested branch `feat/phase-0-foundation` and location `.worktrees/phase-0-foundation`. All tasks below execute there.
- [x] Point dataset commands explicitly at the original checkout's `archive/` with `--archive`; do not copy 2.2 GB into the worktree or depend on untracked data being present there.

## Task 1 — streaming source readiness report

**Files:**

- Create: `pyproject.toml`, `.python-version`, `uv.lock`.
- Create: `workloads/reckoner/pyproject.toml`.
- Create: `workloads/reckoner/src/reckoner/__init__.py`.
- Create: `workloads/reckoner/src/reckoner/data/__init__.py`.
- Create: `workloads/reckoner/src/reckoner/data/profile.py`.
- Create: `workloads/reckoner/tests/test_profile.py`.
- Create: `docs/data/source-readiness.md`.

**Interfaces:**

- Consumes: paths to existing source CSVs, with no edits to source files.
- Produces: `profile_transactions(path: Path, card_keys: set[tuple[str, str]]) -> dict[str, object]`.
- Produces: `inventory(archive: Path) -> dict[str, object]` with file hashes/counts, joins, transaction summary, and readiness blockers.
- CLI: `python -m reckoner.data.profile --archive PATH --output PATH`; explicit output path under ignored `artifacts/`; exit 0 when inventory was generated, 2 for unreadable/missing files. Readiness blockers are data in the report, not an excuse to omit the report.

- [x] **Step 1: Add only the package/test tooling needed by this task.** Use Python 3.12. Root `pyproject.toml` begins with the following configuration; generated lockfile pins resolved transitive versions:

```toml
[project]
name = "touchstone-workspace"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = []

[tool.uv]
package = false

[tool.uv.workspace]
members = ["workloads/reckoner"]

[dependency-groups]
dev = ["pytest>=8,<10", "ruff>=0.11,<1", "jsonschema[format]>=4,<5", "httpx>=0.28,<1"]

[tool.pytest.ini_options]
testpaths = ["workloads/reckoner/tests", "contracts/tests"]

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

The Reckoner package configuration is:

```toml
[project]
name = "reckoner"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = []

[build-system]
requires = ["hatchling>=1.26,<2"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/reckoner"]
```

Write `3.12` to `.python-version`. Add FastAPI/Uvicorn only in Task 4. Resolve/install through `uv sync --all-packages`, not global pip. Package/bootstrap configuration is necessary test infrastructure; no profiler code precedes the red test.

- [x] **Step 2: Write red tests with fabricated CSVs.** The fixture needs no downloaded dataset:

```python
import csv

from reckoner.data.profile import profile_transactions

HEADERS = ["User", "Card", "Year", "Month", "Day", "Time", "Amount",
           "Use Chip", "Merchant Name", "Merchant City", "Merchant State",
           "Zip", "MCC", "Errors?", "Is Fraud?"]


def write_rows(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(HEADERS)
        writer.writerows(rows)


def row(amount="$20.01", label="No", day="1", card="0"):
    return ["0", card, "2020", "1", day, "12:30", amount,
            "Swipe Transaction", "merchant-1", "X", "Y", "", "1234", "", label]


def test_retains_duplicate_looking_records_and_counts_fraud(tmp_path):
    path = tmp_path / "transactions.csv"
    write_rows(path, [row(), row(), row(label="Yes")])
    result = profile_transactions(path, {("0", "0")})
    assert result["row_count"] == 3
    assert result["legitimate_count"] == 2
    assert result["fraud_count"] == 1
    assert result["positive_amount_total_minor"] == 6003


def test_reports_invalid_inputs_without_losing_rows(tmp_path):
    path = tmp_path / "transactions.csv"
    write_rows(path, [row(amount="$0.00"), row(amount="$-1.00"),
                      row(amount="unknown"), row(label="Maybe"),
                      row(day="32"), row(card="missing")])
    result = profile_transactions(path, {("0", "0")})
    assert result["row_count"] == 6
    assert result["nonpositive_amount_count"] == 2
    assert result["invalid_amount_count"] == 1
    assert result["unknown_label_count"] == 1
    assert result["invalid_timestamp_count"] == 1
    assert result["unmatched_card_count"] == 1
```

Add fixture tests for a quoted amount containing a comma, missing required headers, an empty input, and a users CSV containing names but no explicit ID. Assert the inventory marks the user join `unverified` instead of joining by row position. Missing headers raise `ValueError` naming the missing columns without printing source row values.

- [x] **Step 3: Run the new tests and observe failure.**

```bash
uv run --all-packages pytest workloads/reckoner/tests/test_profile.py -q
```

Expected: import failure for the unimplemented profiler. Fix tool-installation errors separately; they are not the intended red.

- [x] **Step 4: Implement the streaming profiler.** Use `csv.DictReader`, `decimal.Decimal`, `datetime`, `hashlib`, and `pathlib`; no pandas or full-table materialization. The money parser follows:

```python
from decimal import Decimal, InvalidOperation


def amount_minor(raw: str) -> int:
    value = Decimal(raw.strip().removeprefix("$").replace(",", "")) * 100
    if not value.is_finite() or value != value.to_integral_value():
        raise ValueError("amount is not a finite whole number of cents")
    return int(value)
```

Catch `InvalidOperation` and `ValueError` in the per-row profiling loop to increment invalid counters. Count each input record once even when several validations fail. Count invalid conditions independently. Collect row counts, labels, positive-value totals, naive time range, channel counts, unmatched card references, negative/zero counts, source checksum, and per-user total/fraud counts. Only dimension key sets and small per-user summaries may reside in memory; no list of transaction rows. Hash source files in bounded binary chunks.

Do not assign a timezone or currency from the sample. Report `timezone=unverified`, `currency=unverified`, and the `$` formatting observation. Do not enrich with current-age/FICO/dark-web attributes: their historical availability is unverified. The CLI prints aggregate counts and blockers, not personal fields. All generated source provenance has `simulated=true`.

The inventory includes `User0_credit_card_transactions.csv` as a separate file with its checksum and `included_in_main_counts=false`. It is never concatenated with the main file. The CLI creates only the requested output's parent directory with `Path.mkdir(parents=True, exist_ok=True)` and writes aggregate JSON there; it never writes within the source directory.

- [x] **Step 5: Run tests and lint, then profile the real corpus without modifying it.**

```bash
uv run --all-packages pytest workloads/reckoner/tests/test_profile.py -q
uv run ruff check workloads/reckoner
uv run ruff format --check workloads/reckoner
uv run --all-packages python -m reckoner.data.profile --archive /Users/bohdanburukhin/Projects/personal/touchstone/archive --output artifacts/source-inventory.json
```

Record elapsed time and peak RSS using the host's available measurement tool. Full-file scanning may take minutes; keep communicating while it runs. Record actual counts in `docs/data/source-readiness.md`, together with hashes, observed formats, unresolved joins/timezone/currency, and the existing source limitation. Consult dataset-author documentation before claiming row-index identity. If no authoritative mapping exists, keep demographic enrichment blocked and use transaction/card references only.

No undocumented normalization assumption may silently advance to a ready cohort. Report remaining empirical ambiguities to the human at the task gate; independent contract work can proceed.

- [x] **Step 6: Commit and request task review.** Stage the explicit Task 1 files, excluding generated inventory and dataset. Suggested commit: `feat: add streaming simulated dataset inventory`. Review input handling and memory behavior before Task 2.

## Task 2 — canonical runtime and measurement contracts

**Files:**

- Create: `contracts/schemas/transaction-v1.schema.json`, `measurement-v1.schema.json`.
- Create: `contracts/examples/transaction-v1.json`, `measurement-v1.json`.
- Create: `contracts/tests/test_events.py`.
- Create: `workloads/reckoner/src/reckoner/contracts.py`.
- Modify: `workloads/reckoner/pyproject.toml` to add `jsonschema[format]>=4,<5`.

**Interfaces:**

- Consumes: approved spec §6; no dataset-specific field names outside the profiler/adapter.
- Produces: `validate_document(document: dict, schema_path: Path) -> None`, raising `jsonschema.ValidationError` for violations.
- Produces: `content_id(document: dict) -> str`, a SHA-256 hex digest of canonical UTF-8 JSON (sorted keys, compact separators, non-finite numbers rejected).
- Produces: versioned schema artifacts usable independently by any JSON Schema implementation; Touchstone never imports the Python helper in Reckoner.

- [x] **Step 1: Write red contract tests using examples defined by the spec.**

```python
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from reckoner.contracts import content_id, validate_document

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "schemas"
EXAMPLES = ROOT / "contracts" / "examples"


def test_canonical_transaction_accepts_simulated_fixture():
    event = json.loads((EXAMPLES / "transaction-v1.json").read_text())
    validate_document(event, SCHEMAS / "transaction-v1.schema.json")


@pytest.mark.parametrize("change", ["missing_tenant", "oracle", "naive_time"])
def test_runtime_event_rejects_leakage_and_missing_context(change):
    event = json.loads((EXAMPLES / "transaction-v1.json").read_text())
    if change == "missing_tenant":
        event.pop("tenant_id")
    elif change == "oracle":
        event["label"] = "fraud"
    else:
        event["occurred_at"] = "2020-01-01T12:30:00"
    with pytest.raises(ValidationError):
        validate_document(event, SCHEMAS / "transaction-v1.schema.json")


def test_source_positions_distinguish_identical_looking_transactions():
    first = {"source_sha256": "a" * 64, "source_record": 1}
    second = {"source_sha256": "a" * 64, "source_record": 2}
    assert content_id(first) != content_id(second)
    assert content_id(first) == content_id(dict(reversed(list(first.items()))))
```

Add tests rejecting floating-point `amount_minor`, an absent schema version, and `provenance.simulated=false` for the CCTD fixture. Generic measurement tests accept two unrelated workflow IDs, nullable correctness for a pending result, unavailable cost without a numeric amount, and a metric contribution with both numerator and denominator. Reject unknown event kinds, missing tenant, negative token counts, and negative duration. Do not require fraud-specific fields in a generic measurement.

- [x] **Step 2: Run the tests and observe the missing implementation/schema failures.**

```bash
uv run --all-packages pytest contracts/tests/test_events.py -q
```

- [x] **Step 3: Implement the two small helpers and the schemas.**

```python
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


def content_id(document: dict) -> str:
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_document(document: dict, schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
```

Both schemas use Draft 2020-12, explicit required fields, and `additionalProperties=false`. The transaction schema implements every field in spec §6.1: identifiers are nonempty strings, `amount_minor` is an integer, currency is three uppercase letters, time is `date-time`, optional entities/locations allow null, and provenance is a strict object. No `label`, raw card number, CVV, or arbitrary source-column bag is accepted. A fabricated fixture may explicitly use `currency=USD` and UTC with `normalization_assumptions=["fabricated fixture uses USD and UTC"]`; that fixture does not settle source normalization.

The measurement schema uses an envelope with identities, schema version, occurrence timestamp, `event_kind`, `simulated`, and one payload object. Allowed kinds: `execution`, `provider_usage`, `outcome`, `evaluation`, `review`, `metric_contribution`. Use `oneOf` branches keyed by `event_kind` to require only the applicable payload fields from spec §6.4. Cost amounts use decimal strings and a cost-status enum; unavailable cost requires a null amount. Correctness allows null. Numeric metric contributions include explicit numerator/denominator and definition version. Required `tenant_id` is at the envelope level for all kinds.

This defines the logical payload, not an ad hoc ingestion endpoint. Actual serialization to spans/events and pinned OTel attribute names are Phase 2 integration work. Do not implement an alternative to OTLP.

- [x] **Step 4: Verify schema and profiler compatibility.**

```bash
uv run --all-packages pytest contracts/tests/test_events.py workloads/reckoner/tests/test_profile.py -q
uv run ruff check contracts/tests workloads/reckoner
uv run ruff format --check contracts/tests workloads/reckoner
```

Expected: all tests pass; canonical fixtures have no source column names or oracle fields. Check schemas using the validator's `check_schema`, already exercised by every positive fixture.

- [x] **Step 5: Commit and request task review.** Suggested commit: `feat: define canonical transaction and measurement contracts`. Review strictness versus legitimate optional fields, provenance, and independent workflow support.

## Task 3 — configuration and cohort manifest versioning

**Files:**

- Create: `contracts/schemas/threshold-config-v1.schema.json`, `cohort-manifest-v1.schema.json`.
- Create: `contracts/examples/threshold-config-v1.json`, `cohort-manifest-v1.json`.
- Create: `contracts/tests/test_manifests.py`.
- Modify: `workloads/reckoner/src/reckoner/contracts.py` with the domain-specific manifest validation functions below.
- Create: `docs/data/cohort-recipe.md`.

**Interfaces:**

- Consumes: `validate_document(document, schema_path)` and `content_id(document)` from Task 2.
- Produces: `validate_threshold_config(document: dict, schema_path: Path) -> None` and `validate_cohort_manifest(document: dict, schema_path: Path) -> None`; schema failures raise `ValidationError`, cross-field failures raise `ValueError`.
- `config_id` and `manifest_id` are computed over each document excluding its own identity field. Configurations contain `tenant_id` and immutable parameters. Manifests contain `tenant_id`, shared `cohort_id`, experiment purpose, source hashes, seed, temporal boundaries, inclusion method, selection counts, graph/history coverage, and dataset-normalization version.

- [x] **Step 1: Write red tests for defaults, identity, and temporal leakage.**

```python
import json
from pathlib import Path

import pytest

from reckoner.contracts import content_id, validate_threshold_config

ROOT = Path(__file__).resolve().parents[2]


def test_config_identity_changes_when_business_assumption_changes():
    original = {"review_cost": "4.00", "margin_rate": "0.30", "tenant_id": "a"}
    changed = {**original, "review_cost": "5.00"}
    assert content_id(original) != content_id(changed)


def test_rejects_inverted_thresholds_even_with_fresh_identity():
    document = json.loads((ROOT / "contracts/examples/threshold-config-v1.json").read_text())
    document["parameters"]["t_low_floor"] = "0.95"
    document["config_id"] = content_id({k: v for k, v in document.items() if k != "config_id"})
    with pytest.raises(ValueError, match="threshold"):
        validate_threshold_config(document, ROOT / "contracts/schemas/threshold-config-v1.schema.json")
```

Add explicit tests that stale IDs fail after parameter edits; the unchanged default document passes; `amount_aware=false` with `t_high<=0.05` fails; a cohort with `history_end >= evaluation_start` fails; a tenant manifest missing source checksums fails; and combined fixture manifests with duplicate selected transaction IDs fail in the test's union check.

- [x] **Step 2: Run the new tests and confirm failure.**

```bash
uv run --all-packages pytest contracts/tests/test_manifests.py -q
```

- [x] **Step 3: Add schemas and cross-field checks.** Use decimal strings for configuration values and `Decimal` for comparisons; preserve the six approved settings exactly. The default example is:

```json
{
  "review_cost": "4.00",
  "margin_rate": "0.30",
  "t_low_floor": "0.005",
  "t_low_ceiling": "0.05",
  "t_high": "0.90",
  "amount_aware": true
}
```

Wrap those parameters with schema version, `tenant_id`, `currency`, and the computed ID. Generate the actual digest from the helper; no placeholder digest is acceptable in committed examples. Cross-field validation follows:

```python
from decimal import Decimal


def validate_threshold_config(document: dict, schema_path: Path) -> None:
    validate_document(document, schema_path)
    body = {key: value for key, value in document.items() if key != "config_id"}
    if document["config_id"] != content_id(body):
        raise ValueError("config identity does not match contents")
    parameters = document["parameters"]
    if Decimal(parameters["review_cost"]) < 0:
        raise ValueError("negative review cost")
    if not Decimal(0) <= Decimal(parameters["margin_rate"]) <= Decimal(1):
        raise ValueError("margin rate is outside [0,1]")
    low = Decimal(parameters["t_low_floor"])
    ceiling = Decimal(parameters["t_low_ceiling"])
    high = Decimal(parameters["t_high"])
    if not Decimal(0) <= low <= ceiling < high <= Decimal(1):
        raise ValueError("invalid threshold ordering")
    if not parameters["amount_aware"] and high <= Decimal("0.05"):
        raise ValueError("flat threshold must be below high threshold")
```

The schema constrains review cost to a nonnegative decimal string and margin to [0,1] through a numeric semantic check using `Decimal`. Require `currency` and prohibit mutation by treating a changed document as a new ID; do not build configuration storage or admin APIs yet.

`validate_cohort_manifest` uses the following checks after schema validation. The schema requires aware `date-time` strings, nonnegative integer counts, nonempty source hashes, `selected_transaction_ids` as a unique array, and all metadata fields listed in this task's interface.

```python
from datetime import datetime


def validate_cohort_manifest(document: dict, schema_path: Path) -> None:
    validate_document(document, schema_path)
    body = {key: value for key, value in document.items() if key != "manifest_id"}
    if document["manifest_id"] != content_id(body):
        raise ValueError("manifest identity does not match contents")
    history_end = datetime.fromisoformat(document["history_end"])
    start = datetime.fromisoformat(document["evaluation_start"])
    end = datetime.fromisoformat(document["evaluation_end"])
    if not history_end < start <= end:
        raise ValueError("invalid temporal boundary")
    counts = document["counts"]
    if counts["fraud"] + counts["legitimate"] != counts["total"]:
        raise ValueError("class counts do not sum to total")
    if len(document["selected_transaction_ids"]) != counts["total"]:
        raise ValueError("selected IDs do not match total")
```

Tenant-scoped manifests share the cohort ID; cross-tenant uniqueness/count checks happen when building the real cohort in Phase 1. Example manifests are explicitly `purpose=fixture`, not fake evidence of the 1,000-case run. Use a one-case fabricated example with `history_end=2019-12-31T23:59:00Z`, `evaluation_start=2020-01-01T00:00:00Z`, `evaluation_end=2020-01-02T00:00:00Z`, and counts `total=1`, `fraud=0`, `legitimate=1`. Its source hash identifies the fabricated fixture, not a downloaded file.

Pin the temporal failure with this additional red test before implementing the validator:

```python
from reckoner.contracts import validate_cohort_manifest


def test_manifest_rejects_history_overlapping_holdout():
    document = json.loads((ROOT / "contracts/examples/cohort-manifest-v1.json").read_text())
    document["history_end"] = document["evaluation_start"]
    document["manifest_id"] = content_id({k: v for k, v in document.items() if k != "manifest_id"})
    with pytest.raises(ValueError, match="temporal"):
        validate_cohort_manifest(document, ROOT / "contracts/schemas/cohort-manifest-v1.schema.json")
```

- [x] **Step 4: Write the executable sampling recipe as documentation, with no source extraction yet.** `docs/data/cohort-recipe.md` specifies these ordered operations:

1. Resolve currency/timezone and card/user reference evidence from Task 1; do not attach unverified user attributes.
2. Freeze a pre-holdout calibration interval and later holdout interval; determine actual dates from inventory coverage, and record the selection rationale before scoring.
3. Select complete user/card histories needed to retain all source fraud; add legitimate entities under measured storage/graph limits. Preserve all retained histories on disk.
4. Assign whole user histories to tenants using pre-holdout activity, aiming at the agreed approximate 70/30 transaction split and risk-profile difference; report achieved distributions. Shared merchant identity does not transfer ownership of a transaction.
5. Among eligible positive-value holdout purchases, stratify by label and select 100 fraud plus 900 legitimate using a declared seed and stable source-record IDs. Sample without replacement. Keep labels in the oracle artifact, never the runtime transaction artifact.
6. Emit per-tenant immutable manifests with the shared cohort ID; union counts must be exactly 1,000/100/900 and transaction IDs unique.
7. Construct graph/retrieval inputs with per-transaction temporal cutoffs; static graphs of full histories cannot serve earlier decisions.
8. Report entity inclusion, full source/retained/evaluation counts, unsupported-record counts, and any graph subset. Do not infer population metrics from a single class ratio.

Do not invent dates, tenant proportions, or resource fit before observing the corpus. The recipe is the Phase 0 deliverable; Phase 1 implements it and freezes the real manifest before any model run.

- [x] **Step 5: Run the contract suite and commit.**

```bash
uv run --all-packages pytest contracts/tests -q
uv run ruff check contracts/tests workloads/reckoner
uv run ruff format --check contracts/tests workloads/reckoner
```

Suggested commit: `feat: version experiment and threshold manifests`. Review temporal semantics, aggregate count ownership, and separation of fixture manifests from measured artifacts.

## Task 4 — minimal API, both deployment paths, and resource evidence

**Files:**

- Create: `workloads/reckoner/src/reckoner/app.py`, `workloads/reckoner/tests/test_app.py`.
- Modify: `workloads/reckoner/pyproject.toml`, `uv.lock`.
- Create: `.dockerignore`, `infra/Dockerfile.reckoner`, `infra/compose.yaml`.
- Create: `infra/kind.yaml`, `infra/k8s/namespace.yaml`, `infra/k8s/reckoner.yaml`.
- Create: `.github/workflows/ci.yaml`, `README.md`.
- Create: `docs/architecture/workspace.dsl`, `docs/architecture/foundation.md`, `docs/operations/local-runtime.md`.

**Interfaces:**

- Consumes: the installable Reckoner package and dependency lockfile.
- Produces: `reckoner.app:app`, `GET /health/live` → 200 `{"status":"ok"}`.
- This is liveness only; it makes no database/model/readiness claim and needs no tenant because it emits no application record.
- Compose endpoint: `http://127.0.0.1:8000/health/live`.
- kind endpoint: port-forward the namespaced service to `127.0.0.1:8000`; never expose a public interface.

- [x] **Step 1: Write the failing application test.**

```python
from fastapi.testclient import TestClient

from reckoner.app import app


def test_liveness_is_available_without_data_or_credentials():
    with TestClient(app) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

Add `fastapi>=0.115,<1` and `uvicorn>=0.30,<1` as Reckoner dependencies; `httpx` is already a test dependency. Run `uv sync --all-packages`, then `uv run --all-packages pytest workloads/reckoner/tests/test_app.py -q`. Expected red: missing `reckoner.app`, not a network request.

- [x] **Step 2: Implement the minimal service.**

```python
from fastapi import FastAPI

app = FastAPI(title="Reckoner", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}
```

Run the test to green. Do not add transaction endpoints, dependency probes, databases, auth scaffolding, or a frontend to make the foundation appear larger.

- [x] **Step 3: Build a locked, non-root container.** `.dockerignore` excludes `archive`, `artifacts`, `.git`, `.worktrees`, `.venv`, `.env*`, caches, and frontend dependency directories. Use a multi-stage build based on `python:3.12-slim` and the installed uv release `0.11.32`, recording resolved image digests in the runtime report. The essential build/runtime instructions are:

```dockerfile
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY workloads/reckoner/ workloads/reckoner/
RUN uv sync --frozen --no-dev --all-packages --no-editable

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "reckoner.app:app", "--host", "0.0.0.0", "--port", "8000"]
```

Use the same resolved Python base image for both stages. Confirm published tags and native host-architecture support before pulling. A missing image tag is an execution prerequisite to resolve, not permission to switch to `latest`. No dataset or secrets enter the build context.

- [x] **Step 4: Add the minimal Compose service and verify it.**

```yaml
services:
  reckoner:
    image: touchstone-reckoner:phase0
    build:
      context: ..
      dockerfile: infra/Dockerfile.reckoner
    ports:
      - "127.0.0.1:8000:8000"
    mem_limit: 256m
    cpus: 0.5
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]
      interval: 5s
      timeout: 3s
      retries: 6
```

Read Docker daemon limits first. Stop for resource discussion if the host cannot support the small test; do not allocate all available RAM or adjust Docker settings without authorization. Run:

```bash
docker compose -p touchstone-foundation -f infra/compose.yaml config --quiet
docker compose -p touchstone-foundation -f infra/compose.yaml up --build -d --wait
curl --fail http://127.0.0.1:8000/health/live
docker stats --no-stream
docker compose -p touchstone-foundation -f infra/compose.yaml down
```

Record only this project's service metrics from `docker stats`. Expected: health response exactly matches the test; the built package runs without source mounted. `down` applies only to this named, task-created Compose project and omits volume deletion.

- [x] **Step 5: Add kind configuration and the API Deployment/Service.** Install kind from its official distribution only after checking the available host architecture and compatible release. Do not change any existing Kubernetes context or cluster configuration. `infra/kind.yaml` contains:

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
```

Create namespace `touchstone-foundation`. In `infra/k8s/reckoner.yaml`, define one Deployment replica and a ClusterIP Service, both with `app: reckoner` labels and that explicit namespace. Use image `touchstone-reckoner:phase0`, `imagePullPolicy: Never`, port 8000, liveness/readiness HTTP probes at `/health/live`, CPU/memory requests `100m`/`64Mi`, and limits `500m`/`256Mi`. Set `runAsNonRoot: true`, `runAsUser: 10001`, and `allowPrivilegeEscalation: false`; the app does not require filesystem writes or privileges.

The probes only establish service liveness in this phase, not future database readiness. Use a dedicated kubeconfig file in ignored `artifacts/`; do not overwrite the user's default kubeconfig. With Compose stopped, execute:

```bash
kind create cluster --name touchstone-foundation --config infra/kind.yaml --kubeconfig artifacts/kind-kubeconfig --wait 60s
kind load docker-image touchstone-reckoner:phase0 --name touchstone-foundation
kubectl --kubeconfig artifacts/kind-kubeconfig apply -f infra/k8s/namespace.yaml
kubectl --kubeconfig artifacts/kind-kubeconfig apply -f infra/k8s/reckoner.yaml
kubectl --kubeconfig artifacts/kind-kubeconfig -n touchstone-foundation rollout status deployment/reckoner --timeout=60s
kubectl --kubeconfig artifacts/kind-kubeconfig -n touchstone-foundation port-forward service/reckoner 8000:8000 --address 127.0.0.1
```

Keep port-forward in a controlled terminal session; from a second session run the same `curl --fail` health check. Record pod readiness, image ID, node resource allocation, and actual container memory. Terminate only the task's port-forward process. Delete only this disposable cluster after verifying its exact name and that it was created by this task; preserve its measured results in the report. Never run `docker system prune` or delete unrelated clusters/volumes.

- [x] **Step 6: Add CI using the local checks, without paid calls or the dataset.** Pin third-party action revisions when creating the workflow. On push/pull request, check out code, set up the chosen uv/Python versions, and run:

```bash
uv sync --all-packages --frozen
uv run --all-packages pytest -q
uv run ruff check .
uv run ruff format --check .
docker compose -p touchstone-foundation -f infra/compose.yaml config --quiet
docker build -f infra/Dockerfile.reckoner -t touchstone-reckoner:phase0 .
```

Unit/contract tests require only fabricated fixtures. Full source profiling and local kind tests are explicit local checks, not mandatory hosted-CI jobs that download the dataset. No Anthropic/Jev/Snowflake secret is requested by this phase.

- [x] **Step 7: Document and check the actual architecture and resource envelope.** `README.md` links the approved spec, this plan, source-readiness report, and local runbook, states the simulated-data limitation, and gives tested commands. `docs/operations/local-runtime.md` records host architecture, reported versus measured disk, Docker memory/CPU limits, versions/image IDs, profiler RSS/runtime, Compose API memory, kind node memory, and that these numbers do not prove the full stack fits.

Create a Structurizr context/container model with Touchstone, Reckoner, browser, providers, and stores. Tag future containers `Planned`; mark only the health API and profiler as implemented. A separate foundation view shows only components actually shipped in this phase. `docs/architecture/foundation.md` explains the OTLP boundary and links the DSL. Add a short Mermaid sequence for the data inventory flow; do not hand-draw a LangGraph graph before it exists.

Read/apply `archify` for the required phase-end drift check, comparing diagrams to code. It is an inspection of delivered scope, not permission to build the planned containers. Record discrepancies and resolve diagram drift before completion.

- [x] **Step 8: Run final checks once and review the branch.**

```bash
uv run --all-packages pytest -q
uv run ruff check .
uv run ruff format --check .
git diff --check
git status --short
git ls-files archive artifacts .env
```

The last command must produce no tracked dataset, generated local state, or secret files. Local Compose/kind evidence comes from Steps 4–5; rerun those only if related changes invalidate the evidence. Record blockers explicitly rather than claiming a pass.

Suggested commit: `feat: validate foundation API in compose and kind`. Obtain whole-branch code review after task-level reviews, then use `superpowers:finishing-a-development-branch`. Do not infer permission to push, publish, merge, or begin Phase 1 from finishing this plan.

## Phase 0 completion checklist

- [x] First commit and isolated implementation worktree exist; original untracked data remains intact.
- [x] Unit and contract tests passed from a locked environment after observed red phases.
- [x] Main corpus profile reports actual counts, invalid values, join evidence, and memory/runtime without loading all transactions into memory.
- [x] Currency/timezone/user-join assumptions are either supported by evidence or explicitly unresolved gates before cohort construction.
- [x] Contracts reject missing tenants and oracle leakage; generic measurement supports independent workflows.
- [x] Config/cohort identities and temporal validation are tested; the actual evaluation cohort has not been falsely claimed as built.
- [x] The same API image passed Compose and kind health checks in separate runs.
- [x] Disk and Docker resource allocation are measured; full-stack feasibility remains unproven.
- [x] Current versus planned architecture is clearly distinguished and checked for drift.
- [x] No Jev stub, paid model call, database stack, frontend scaffolding, or production claim was introduced.
- [x] Task reviews and whole-branch review are complete; all reported findings are resolved.
- [ ] Human selects branch integration or preservation; no merge or push has occurred.

## Documentation consulted while planning

- [uv workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/) — shared lockfile, member packages, and workspace command behavior.
- [kind quick start](https://kind.sigs.k8s.io/docs/user/quick-start/) — local cluster creation and loading an already-built image.
- [Docker Compose startup and health checks](https://docs.docker.com/compose/how-tos/startup-order/) — health-aware service startup.

## Review and handoff

The approved spec authorizes planning. This written plan still requires review before implementation, as required by the Superpowers writing-plans handoff. Execution method is already chosen: fresh subagent per implementation task, with task and branch review. Ask whether this plan captures the intended Phase 0 scope; do not ask the user to choose the execution method again.


## Execution record — 2026-09-25

Implementation branch: `feat/phase-0-foundation`, based on `f11c76f` on `main`.
Implementation commits: `ddb2301`, `8a9a2db`, `cd9316f`, `8ef81a4`, `809bc0f`,
`2fcfab7`, and `c124698`. All four tasks passed independent task review. Whole-branch
review found an outcome-currency omission and two minor validation/coverage gaps;
the single final fix wave resolved all three, with scoped re-review approval.

Final verification at `c124698`: 58 tests passed; Ruff lint and formatting passed;
`git diff --check` passed; no dataset, generated artifacts, or secret files tracked.
The same non-root image passed the health check separately in Docker Compose and kind.
Task-created containers, cluster, and port-forward were removed. Archify’s evidence-backed
phase view passed 9/9 validation checks, with zero errors and warnings.

The streaming inventory scanned 24,386,900 simulated transactions, including 29,757
fraud labels, in 111.06 seconds at 23,822,336-byte peak RSS. The source-readiness report
contains counts and hashes. Currency, timezone, user-table mapping, and historical
attribute availability remain explicit gates for subsequent normalization/enrichment.

Hosted CI has not run. The Structurizr DSL was structurally checked but not parsed or
rendered with the Structurizr CLI. Measurements cover only the delivered foundation,
not the full future stack. No paid model/provider call was made.

Two recorded execution rulings:

1. Update `uv.lock` with Task 2’s dependency change despite its omission from that
   task’s file list. This preserves reproducibility; the cost if wrong is a small,
   reversible lockfile diff.
2. Replace the planned development-only `httpx` dependency with `httpx2>=2,<3`
   because the installed Starlette TestClient and its official upstream now prefer
   `httpx2` and warn on the old fallback. No warning suppression was added. The cost
   if wrong is a reversible development-dependency and lockfile change. Evidence:
   [Starlette TestClient source](https://github.com/Kludex/starlette/blob/main/starlette/testclient.py).

The repository’s `main` checkout contains only the initial documentation baseline.
Implementation remains in `.worktrees/phase-0-foundation` until the human chooses its
integration. Later phases require their own plans; Phase 1 has not begun.

# Touchstone

Touchstone measures instrumented decision workflows; Reckoner is its first simulated payments-risk
workload. **All transaction data is simulated.** Phase 1 provides a sequential baseline CLI,
Postgres persistence and spend reservations, read-only operational API, oracle evaluation,
JSON/Markdown reports, and durable OTLP export. Phase 2 replays the saved exports into a local
collector, ClickHouse, governed DuckDB marts and a Next.js dashboard. Paid measurement is tracked
separately from software and fabricated deployment checks.

- [Approved foundation](docs/spec/000-foundation.md) and [Phase 1 specification](docs/spec/001-reckoner-v0.md)
- [Architecture and current/planned boundaries](docs/architecture/foundation.md)
- [Baseline operations, credential separation, recovery and budget gates](docs/operations/baseline-runbook.md)
- [Runtime resources](docs/operations/local-runtime.md) and [completed Phase 1 baseline](docs/evidence/phase-1-completion.md)
- [Original failed pilot evidence](docs/evidence/phase-1-baseline.md) (preserved history)
- [Phase 2 local evidence](docs/evidence/phase-2-touchstone.md) and [platform runbook](docs/operations/platform-runbook.md)

Install the locked environment and run checks:

```bash
uv sync --all-packages --frozen
uv run --frozen --all-packages dbt deps --project-dir platform/dbt
uv run --frozen --all-packages pytest -m 'not integration and not clickhouse_integration' -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
```

Postgres integration tests require a disposable Postgres 17 instance and
`RECKONER_TEST_OWNER_DSN` with permission to create temporary databases/login roles; run
`uv run --frozen --all-packages pytest -m integration -q`. ClickHouse ingestion integration is
marked `clickhouse_integration` and needs a separate disposable raw store. CI keeps these jobs
separate and runs fabricated image smokes; no local CCTD archive or paid provider access is
required.

The [baseline runbook](docs/operations/baseline-runbook.md) gives the Compose and separate kind
procedures. Both use the same non-root image, protected role credentials, persistent Postgres,
explicit migrations, `/health/live`, and migration-aware `/health/ready`. Smoke uses four fixed
fabricated purchases and the fake provider; missing credentials never activate it.

The platform boundary is OTLP only. The independent synthetic workflow exercises it without
Reckoner imports or provider credentials. DuckDB is the verified local warehouse; the Snowflake
example awaits owner access and live validation. Reckoner's Jev/graph/cascade and reviewer
interface remain later-phase work.

Platform and synthetic-workflow packaging currently supports direct wheels:
`uv build --wheel --package touchstone-platform` and
`uv build --wheel --package touchstone-synthetic`. Their shared schemas are included
from `contracts/` at wheel-build time. The default source-distribution-to-wheel
flow is unsupported because those external schema paths are absent from the
source distribution. Docker uses the supported direct-wheel path; publishing
source distributions requires a separate packaging change.

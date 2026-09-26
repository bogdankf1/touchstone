# Touchstone

Touchstone measures instrumented decision workflows; Reckoner is its first simulated payments-risk
workload. **All transaction data is simulated.** Phase 1 provides a sequential baseline CLI,
Postgres persistence and spend reservations, read-only operational API, oracle evaluation,
JSON/Markdown reports, and durable OTLP export. Paid measurement is tracked separately from
software and fabricated deployment checks.

- [Approved foundation](docs/spec/000-foundation.md) and [Phase 1 specification](docs/spec/001-reckoner-v0.md)
- [Architecture and current/planned boundaries](docs/architecture/foundation.md)
- [Baseline operations, credential separation, recovery and budget gates](docs/operations/baseline-runbook.md)
- [Runtime resources](docs/operations/local-runtime.md) and [Phase 1 evidence](docs/evidence/phase-1-baseline.md)

Install the locked environment and run checks:

```bash
uv sync --all-packages --frozen
uv run --frozen --all-packages pytest -m 'not integration' -q
uv run --frozen ruff check .
uv run --frozen ruff format --check .
```

Integration tests require a disposable Postgres 17 instance and `RECKONER_TEST_OWNER_DSN` with
permission to create temporary databases/login roles. With that set, run the complete
`uv run --frozen --all-packages pytest -q`. CI requires unit, real-Postgres integration, and a
fabricated image smoke; no local CCTD archive or paid provider access is required.

The [baseline runbook](docs/operations/baseline-runbook.md) gives the Compose and separate kind
procedures. Both use the same non-root image, protected role credentials, persistent Postgres,
explicit migrations, `/health/live`, and migration-aware `/health/ready`. Smoke uses four fixed
fabricated purchases and the fake provider; missing credentials never activate it.

Touchstone's collector, ClickHouse, governed marts and dashboard, plus Reckoner's Jev/graph/cascade
and reviewer interface, remain scheduled for later phases. The platform boundary is OTLP only.

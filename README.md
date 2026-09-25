# Touchstone

Touchstone is a measurement platform for instrumented decision workflows; Reckoner is its first
simulated payments-risk workload. **All transaction data in this repository is simulated.** Phase
0 provides contracts, source profiling, a minimal liveness API, and local deployment evidence. It
does not provide transaction processing, model calls, databases, or a frontend.

## Foundation documents

- [Approved foundation specification](docs/spec/000-foundation.md)
- [Approved Phase 0 implementation plan](docs/superpowers/plans/2026-09-25-phase-0-foundation.md)
- [Simulated source-readiness report](docs/data/source-readiness.md)
- [Foundation architecture](docs/architecture/foundation.md)
- [Local runtime and measured resources](docs/operations/local-runtime.md)

## Verified checks

Install the locked Python environment and run its tests and formatting checks:

```bash
uv sync --all-packages --frozen
uv run --all-packages pytest -q
uv run ruff check .
uv run ruff format --check .
```

Build and exercise the minimal API with Docker Compose:

```bash
docker compose -p touchstone-foundation -f infra/compose.yaml up --build -d --wait
curl --fail http://127.0.0.1:8000/health/live
docker compose -p touchstone-foundation -f infra/compose.yaml down
```

The same image has also been verified under a disposable single-node kind cluster. The exact
installation, deployment, evidence, and cleanup commands are in the
[local runtime runbook](docs/operations/local-runtime.md). Compose and kind are run separately.

The liveness endpoint returns `{"status":"ok"}`. It does not claim that future stores, model
providers, or transaction workflows are ready.

# Local runtime and resource evidence

All transaction data referenced here is simulated. Measurements below come from one development
host on 2026-09-25 and are point-in-time observations, not sizing guarantees.

## Environment

| Item | Observed value |
|---|---|
| Host architecture | Apple arm64 |
| Docker Desktop | client/server 29.8.0; Compose v5.5.1 |
| Docker allocation | 12 CPUs; 8,319,238,144 bytes (7.748 GiB) memory |
| Kubernetes client | kubectl v1.36.1; Kustomize v5.8.1 |
| kind | v0.33.0, installed under ignored `artifacts/tools/` |
| Python | host locked environment 3.12.13; image 3.12.14 |
| uv | 0.11.32 |
| Disk | planning inspection about 80.2 GiB free; human-approved budget about 80 GB; task-time `df` showed 77 GiB free |

The image uses the verified native arm64 indices
`python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9`
and
`ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c`.
The built `touchstone-reckoner:phase0` image ID was
`sha256:e0f3c305da524e14c7464ff6de30ce5bee0d1b2d2610fe564918f63ff0c9ce8e`,
reported as arm64 with user `10001:10001`, Python 3.12.14, and 224,497,364 bytes unpacked.

## Measurements

| Check | Actual observation |
|---|---|
| Full simulated-corpus profiler | 111.06 seconds; 23,822,336-byte peak RSS (about 22.7 MiB) |
| Compose API container | healthy; 41.55 MiB of 256 MiB; 0.36% CPU; 2 PIDs |
| kind node | 729.8 MiB of 7.748 GiB at the sample; 12 CPU and 8,124,256 Ki memory allocatable |
| kind Reckoner container | 41,693,184-byte working set; 40,468,480-byte RSS; 100m/64Mi request; 500m/256Mi limit |
| kind pod | Ready 1/1; zero restarts; loaded image ID `sha256:7395c7c30913ef8f59b5f4e53be8a9827e97551d6f54beec0a19dfde4d2ef178` |

Both deployment paths returned exactly `{"status":"ok"}` from `/health/live`. Compose was stopped
before kind started. The named Compose project and disposable kind cluster were removed after the
checks; no volumes or unrelated Docker resources were deleted.

These small-process measurements do not prove that the future databases, model integrations,
workflow runtime, warehouse tooling, and web application fit together within the Docker
allocation. Each later phase must measure only the services it introduces and revisit concurrency
before running more of the stack.

## Phase 1 procedure and limits

The preceding image/resource table is historical Phase 0 evidence. Current Phase 1 commands are
in [baseline-runbook.md](baseline-runbook.md); the old credential-free Compose command is no
longer sufficient because readiness now verifies Postgres migration history.

Phase 1 keeps API/CLI/Postgres limits at 512 MiB each, PostgreSQL `shared_buffers=128MB` and
`max_connections=40`. Only active operations run alongside Postgres and the API. The deployment
image contains its own contracts, prompt, default config, migrations and provenance, and runs as
UID/GID 10001. Postgres 17.11 uses the verified multiarchitecture OCI index:

`postgres:17@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f`

Original Phase 1 deployment, preparation and failed-pilot evidence remains in
[phase-1-baseline.md](../evidence/phase-1-baseline.md). The subsequent native structured-output
revision and completed 1,000-case measured baseline are recorded separately in
[phase-1-completion.md](../evidence/phase-1-completion.md), including the current installed image,
resource samples, budget ledger and backup identities. Fabricated smoke remains separate from
measured provider results. The unchanged original preparation took 926.11 seconds at one CPU,
with observed process peak RSS 347,520 KiB under a 512 MiB limit. Measured services were stopped
after backup verification; their persistent ledger volume remains intact.

The task reused the existing verified kind v0.33.0 binary at
`.worktrees/phase-0-foundation/artifacts/tools/kind` from the root checkout. Its original official
Darwin arm64 checksum is `0c8c7dbe5e23594a198b786c4bc13dacc101fa6196b0cb0b23a1ca44e61f4b4f`.
The task-owned cluster/kubeconfig were separate from the user's configuration. Compose was
stopped before kind. Evidence was copied out before deleting only the smoke cluster.

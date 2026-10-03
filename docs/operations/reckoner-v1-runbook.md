# Reckoner v1 local runtime runbook

All transaction data is simulated. The deployment smoke uses a tiny fabricated simulated
source generated in code (`reckoner.v1.smoke`), makes zero provider calls and proves local
packaging only; it is not a production deployment or a measured experiment. Paid Jev and
Anthropic work still requires a separately approved protocol (see the Phase 3 plan).

## Preserved resources — never attach, reset or delete

| Kind | Identifiers |
| --- | --- |
| Volumes | `touchstone-phase1-measured_postgres-data`, `touchstone-phase1-smoke_postgres-data`, `touchstone-phase2-measured_{clickhouse-data,collector-queue,warehouse-data}`, `touchstone-phase3-task3_task3-{neo4j,postgres}`, `touchstone-phase3-task10_{clickhouse-data,collector-queue}`, `touchstone-phase3-task11-{neo4j,postgres}` |
| Compose projects | `touchstone-phase1-measured`, `touchstone-phase1-smoke`, `touchstone-phase2-measured`, `touchstone-phase3-task3`, `touchstone-phase3-task10` |
| Data paths | `archive/` source CSVs, `artifacts/phase1*`, `artifacts/phase2*`, `artifacts/phase3/{data,task3,task5,task11*}` |

`infra/compose.reckoner-v1.test.yaml` is fixed to project `touchstone-phase3-task3` and is
for CI only; locally it would attach the preserved Task 3 volumes. Do not run it here.
Never run `docker system prune`, `docker volume prune`, `down --volumes` or any global
cleanup. Never mount or read the repository `.env`.

## Images

Build from a committed revision so `build.json` names the exact source:

```bash
docker build -f infra/Dockerfile.reckoner \
  --build-arg RECKONER_BUILD_REVISION="$(git rev-parse HEAD)" -t touchstone-reckoner:phase3 .
docker build -f infra/Dockerfile.web -t touchstone-web:phase3 .
docker build -f infra/Dockerfile.platform -t touchstone-platform:phase3 .
```

The Reckoner image installs only the `reckoner` package and its locked dependencies
(`uv sync --package reckoner`) and runs as UID 10001 with `HOME=/home/reckoner`. The web
image prunes development dependencies after `next build` and sets ownership while copying,
avoiding the duplicate layer of the Phase 2 image. Postgres and Neo4j stay pinned by digest:
`pgvector/pgvector:0.8.6-pg17-bookworm@sha256:cf134a76…f8e6f` and
`neo4j:2026.09.0@sha256:91fb0bf2…edf4e`. The Neo4j GDS plugin is the checksum-verified
GPLv3 JAR (`74e7026e…2a8e`) fetched by `infra/prepare-reckoner-v1-test.sh`.

## Instance, volumes and credentials

An instance name must match `touchstone-phase3-v1-[a-z0-9][a-z0-9-]{2,30}`. Every volume is
external and instance-scoped, so Compose never creates, adopts or removes one:

```bash
export RECKONER_V1_INSTANCE=touchstone-phase3-v1-<name>
for suffix in postgres neo4j neo4j-logs clickhouse collector-queue warehouse; do
  docker volume inspect "$RECKONER_V1_INSTANCE-$suffix" >/dev/null 2>&1 && exit 1  # never adopt
  docker volume create --label "touchstone.smoke.instance=$RECKONER_V1_INSTANCE" \
    "$RECKONER_V1_INSTANCE-$suffix"
done
```

`RECKONER_SECRET_DIR` is a protected directory (mode 0700, files 0600) with
`postgres.env` (`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`), `provision.env`
(owner, runner, evaluator and API DSNs for `reckoner migrate --provision-roles`),
`runner.env`, `api.env`, `neo4j-server.env` (`NEO4J_AUTH`) and `neo4j.env`
(`RECKONER_NEO4J_URI`, `RECKONER_NEO4J_USER`, `RECKONER_NEO4J_PASSWORD`). Export
`TOUCHSTONE_CH_PASSWORD` for every Compose command (the platform services are extended
from `infra/compose.platform.yaml`). Provider keys are injected only for an approved paid
protocol and never into the smoke. The smoke generates all of these freshly.

Common variables: `RECKONER_V1_EVIDENCE_DIR` (writable output), `RECKONER_V1_DATA_DIR`
(read-only preparation bundle and source), `RECKONER_GDS_PLUGIN_DIR`
(`artifacts/phase3/task3/plugins`), and optional `RECKONER_V1_POSTGRES_VOLUME` /
`RECKONER_V1_NEO4J_VOLUME` to point a restore project at new volumes.

## Profiles — one heavy profile at a time

Run exactly one profile, each under its own project, and never alongside the preserved
Phase 2 stack, another Phase 3 store or a kind cluster (8 GB Docker allocation).

```bash
c() { docker compose -p "$RECKONER_V1_INSTANCE-$1" --profile "$1" -f infra/compose.reckoner-v1.yaml "${@:2}"; }

# Preparation/GDS: import, graph, projections, evidence (graph-available evidence persisted).
c prepare up -d --wait postgres neo4j
c prepare run --rm --no-deps -T migrate
c prepare run --rm --no-deps -T -e RECKONER_SOURCE_DIR=/data/source owner \
  reckoner v1 import --bundle /data/bundle --env-file -
c prepare down                      # containers only; volumes persist

# Online: API (ready only after every migration), console, dashboard API, workflow worker.
c online up -d --wait api web platform-api
c online port web 3000
c online down

# Platform refresh (set RECKONER_V1_POSTGRES_MEMORY=512m): outbox -> OTLP -> warehouse.
RECKONER_V1_POSTGRES_MEMORY=512m c refresh up -d --wait postgres clickhouse collector platform-api
c refresh run --rm --no-deps -T exporter reckoner v1 telemetry-export --env-file - \
  --endpoint http://collector:4318
c refresh run --rm --no-deps -T refresh
c refresh down
```

| Profile | Services (memory ceiling) | Ceiling sum |
| --- | --- | --- |
| prepare | Postgres 2 GiB, Neo4j 4 GiB (heap 2g, page cache 512m), one job ≤ 1 GiB | 7 GiB |
| online | Postgres 2 GiB, API 512 MiB, web 512 MiB, platform API 512 MiB, one job ≤ 1 GiB | 4.5 GiB |
| refresh | Postgres 512 MiB, ClickHouse 2 GiB, collector 384 MiB, platform API 512 MiB, refresh 2.5 GiB | 6.375 GiB |

The ceilings are not observed usage. The Postgres 2 GiB and Neo4j 4 GiB settings are the
configuration under which the bounded Task 3 import and Task 11 benchmark ran without an
OOM kill (Task 3 lifetime cgroup peaks reached both limits; Task 11 warm-schedule cgroup
peaks were 2.56 GB Neo4j and 0.67 GB Postgres). They are not proof that the complete
multi-year operational union fits. No profile starts Dagster UI/daemon or duplicates the
Phase 2 5,888 MiB stack; the graph runs only for preparation.

## Health, readiness and stopped services

* `/health/live` reports process liveness only. `/health/ready` returns 503 until **every**
  packaged migration is recorded (migration before traffic); the API's `depends_on` waits for
  the `migrate` job. Readiness never depends on Neo4j.
* With the graph stopped, newly assembled evidence is `partial` with `graph unavailable`;
  the workflow records a degraded escalation without any scorer dispatch. The console shows
  the missing graph coverage and a null graph snapshot (snapshot availability, not live
  service health).
* With Postgres stopped, the API stays live, readiness and data reads return 503, and the
  web proxy returns 503 without leaking credentials.
* With the collector stopped, `telemetry-export` reports `sent: 0` and leaves every outbox
  row pending; the Postgres outbox is the durable queue. The collector keeps its own
  file-backed queue on `$RECKONER_V1_INSTANCE-collector-queue`.

## Backup and restore

Postgres (new records): while the online profile is quiescent,

```bash
c online exec -T postgres pg_dump -U postgres -d "$POSTGRES_DB" -Fc > reckoner-v1.dump
c online exec -T postgres pg_dumpall -U postgres --globals-only --no-role-passwords > globals.sql
```

Restore only into a NEW volume (`RECKONER_V1_POSTGRES_VOLUME`) under a separate project:
apply `globals.sql` without the `postgres` role lines, `pg_restore --exit-on-error`, then
`reckoner migrate --provision-roles` (checksums must match; login passwords are set again).
Compare table fingerprints (`infra/verify_reckoner_v1_smoke.py fingerprint`) before serving.

Graph: Neo4j is not backed up. It is reimported from the immutable source into a new
volume. The smoke proves this for its fabricated source (`reckoner v1 smoke graph`): the
reimported source graph and the deterministic projection fields equal the original. For the
real simulated archive there is no tracked graph-import command yet; Task 3 used a bounded
scratch procedure. Rebuilding the real graph therefore remains a pending delivery gate.

ClickHouse/collector: this runbook does not claim raw ClickHouse or collector-queue disaster
recovery. Only the published-warehouse snapshot procedure in
[platform-runbook.md](platform-runbook.md) has been exercised.

## Compose smoke

```bash
python3 infra/smoke-reckoner-v1.py compose --instance touchstone-phase3-v1-smoke-<id> \
  --evidence-dir artifacts/phase3/task12/compose-<id> \
  --plugin-dir artifacts/phase3/task3/plugins
python3 infra/smoke-reckoner-v1.py cleanup --evidence-dir artifacts/phase3/task12/compose-<id>
```

It refuses a non-matching instance, an existing volume for the instance, a running Phase 2
measured or Phase 3 task stack, any running kind node, and free disk below 15 GiB. Every
resource is written to `ledger.json` before creation and labelled with a random sentinel.
After a failure it stops only its own projects and keeps its own volumes for inspection;
`cleanup` removes only ledger volumes whose sentinel matches. It runs prepare, online
(workflow, verification, Postgres restart, stopped-Postgres check, dump), refresh (outbox,
collector stop/start, warehouse refresh, published-run check), then restore and graph
reimport into new volumes and serves the restored database. Receipts are JSON files in the
evidence directory.

## kind

Compose and kind never run together. The kind path uses its own kubeconfig file, the
explicit context `kind-<instance>` and namespace `touchstone-phase3-v1-smoke`:

```bash
unset KUBECONFIG
python3 infra/smoke-reckoner-v1.py kind --instance touchstone-phase3-v1-kind-<id> \
  --evidence-dir artifacts/phase3/task12/kind-<id> --plugin-dir artifacts/phase3/task3/plugins \
  --kubeconfig artifacts/phase3/task12/kind-<id>.kubeconfig \
  --kind-bin <verified kind v0.33.0> --kind-sha256 0c8c7dbe5e23594a198b786c4bc13dacc101fa6196b0cb0b23a1ca44e61f4b4f
kubectl --kubeconfig artifacts/phase3/task12/kind-<id>.kubeconfig \
  --context kind-touchstone-phase3-v1-kind-<id> -n touchstone-phase3-v1-smoke get pods
```

It refuses the ambient `~/.kube/config`, a `KUBECONFIG` pointing elsewhere and an existing
kubeconfig file. It writes a run-specific kind configuration (node image from
`infra/kind.yaml`, the GDS plugin directory mounted read-only at
`/touchstone/gds-plugins`, and a node sentinel label), tags the pinned Postgres and Neo4j
digests as `touchstone-{pgvector,neo4j}:phase3` after checking image identity, loads
arm64 archives, creates secrets from the generated env files at run time, and applies
`infra/k8s/reckoner-v1/` stage by stage (`-l touchstone.dev/stage=...`). The manifests use
PersistentVolumeClaims for Postgres and Neo4j and the same memory limits as Compose.

## Measured evidence

See the Task 12 evidence section below; numbers there come from the recorded smoke receipts
under ignored `artifacts/phase3/task12/`.

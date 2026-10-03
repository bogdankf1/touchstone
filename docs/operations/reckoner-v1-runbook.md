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
export RECKONER_V1_POSTGRES_VOLUME="$RECKONER_V1_INSTANCE-postgres"
export RECKONER_V1_NEO4J_VOLUME="$RECKONER_V1_INSTANCE-neo4j"
create_v1_volumes() {
  local suffix
  # Check every name first: any existing volume refuses the whole set, leaving no residue.
  for suffix in postgres neo4j neo4j-logs clickhouse collector-queue warehouse; do
    if docker volume inspect "$RECKONER_V1_INSTANCE-$suffix" >/dev/null 2>&1; then
      echo "refusing to adopt existing volume $RECKONER_V1_INSTANCE-$suffix" >&2
      return 1
    fi
  done
  for suffix in postgres neo4j neo4j-logs clickhouse collector-queue warehouse; do
    docker volume create --label "touchstone.smoke.instance=$RECKONER_V1_INSTANCE" \
      "$RECKONER_V1_INSTANCE-$suffix" || return 1
  done
}
create_v1_volumes
```

If the function refuses, choose a new instance name; never point a later profile at
volumes this procedure did not create.

Compose requires `RECKONER_V1_INSTANCE`, `RECKONER_V1_POSTGRES_VOLUME` and
`RECKONER_V1_NEO4J_VOLUME` and has no defaults for them. Compose cannot check names against
the preserved list above; that validation lives in `infra/smoke-reckoner-v1.py` and in this
procedure (instance names must match the pattern, which no preserved volume does).

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
(`artifacts/phase3/task3/plugins`); a restore project points `RECKONER_V1_POSTGRES_VOLUME`
/ `RECKONER_V1_NEO4J_VOLUME` at new volumes.

## Profiles — one heavy profile at a time

Run exactly one profile, each under its own project, and never alongside the preserved
Phase 2 stack, another Phase 3 store or a kind cluster (8 GB Docker allocation).

```bash
c() { docker compose -p "$RECKONER_V1_INSTANCE-$1" --profile "$1" -f infra/compose.reckoner-v1.yaml "${@:2}"; }

# Preparation/GDS. Tracked commands today: migration and the Postgres import only.
# Graph import, GDS projection and evidence assembly for the real archive have no tracked
# command yet (see "Open preparation gate" below); only the fabricated smoke runs them.
# `reckoner v1 import` was not executed in Task 12.
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
| refresh | Postgres 512 MiB, ClickHouse 2 GiB, collector 384 MiB, platform API 512 MiB, one job ≤ 2.5 GiB (refresh; exporter 512 MiB) | 5.875 GiB |

Each sum is the resident services plus the largest one-shot job, because jobs run one at a
time; running the exporter concurrently with the refresh job would make refresh 6.375 GiB,
which the smoke never does. The ceilings are not observed usage. The Postgres 2 GiB and Neo4j 4 GiB settings are the
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
  if a worker is configured with a Neo4j URI but the service is unreachable, evidence records
  `graph unavailable: service unreachable` instead of failing, and the relational arm
  continues. Either way the workflow records a degraded escalation without any scorer
  dispatch; a GDS-augmented run never presents as complete. The console shows
  the missing graph coverage and a null graph snapshot (snapshot availability, not live
  service health).
* With Postgres stopped, the API stays live, readiness and data reads return 503, and the
  web proxy returns 503 without leaking credentials.
* With the collector stopped, `telemetry-export` reports `sent: 0` and leaves every outbox
  row pending; the Postgres outbox is the durable queue. The collector keeps its own
  file-backed queue on `$RECKONER_V1_INSTANCE-collector-queue`.
* Collector readiness: the pinned collector image is distroless (no shell or HTTP client)
  and the platform configuration enables no `health_check` extension, so Compose declares
  no collector healthcheck and `up --wait` only waits for it to be running. Readiness is
  established by a successful OTLP export (`sent` equals the pending count, `pending: 0`).
  In kind the collector has a TCP readiness probe on port 4318. Changing the platform
  collector configuration was out of scope.

## Backup and restore

Postgres (new records): while the online profile is quiescent,

```bash
# Single quotes: $POSTGRES_DB expands inside the container (from postgres.env), not on the host.
c online exec -T postgres sh -c 'pg_dump -U postgres -d "$POSTGRES_DB" -Fc' > reckoner-v1.dump
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

**Open preparation gate (owned by a separate Stage A follow-up task).** For the real
simulated archive there is no tracked command for (1) graph import into Neo4j, (2) GDS
projection, or (3) evidence assembly into `reckoner.v1_evidence`. `assemble_evidence` and
`import_graph` are called only by `reckoner v1 smoke` and the benchmark harness, and
`reckoner v1 score` requires a pinned prepared evidence record. Until those commands exist,
the prepare profile can run only the migration and the Postgres import shown above.

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
measured or Phase 3 task stack, any running kind node, free disk below 15 GiB, and an
evidence directory inside the tracked tree unless git-ignored. Every volume, project and
cluster is written to `ledger.json` before creation; volumes, every Compose container
(`RECKONER_V1_SENTINEL` label) and the kind node carry the run's random sentinel. Loading a
ledger re-validates the instance, sentinel format, the four stage projects, the cluster and
the dedicated kubeconfig. After a failure it stops only its own projects (profile-independent
`down`, never `--volumes`) and keeps its own volumes for inspection. `cleanup` first checks
every ledger volume, project container and cluster for the exact sentinel, then stops and
deletes only those, and finally removes the generated credentials in `secrets/`. Before
reporting success, every receipt, log and dump is scanned for the six generated credentials.
Optional `--derived-baseline-bytes` with `--derived-cap-bytes` (both or neither) add a
derived-data cap to the free-disk guard, which records a checkpoint after each profile. It runs prepare, online
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
kubeconfig file. Cleanup checks that the ledger kubeconfig names the cluster and that the
node carries the run's sentinel label, runs `kind delete cluster --kubeconfig <dedicated>`
(never the ambient configuration), then deletes that kubeconfig file. Optional
`--derived-baseline-bytes` and `--derived-cap-bytes` make the disk guard abort above an
owner-approved Phase 3 derived-data cap as well as below the 15 GiB free-disk floor. It writes a run-specific kind configuration (node image from
`infra/kind.yaml`, the GDS plugin directory mounted read-only at
`/touchstone/gds-plugins`, and a node sentinel label), creates the local tags
`touchstone-{pgvector,neo4j,clickhouse,collector}:phase3` for the pinned digests (an
existing tag naming a different image is refused, never moved), saves archives for the Docker
host's architecture into a private temporary directory, creates secrets from the generated
env files at run time (`--from-env-file`, never on argv), and applies
`infra/k8s/reckoner-v1/` stage by stage (`-l touchstone.dev/stage=...`). The manifests use
PersistentVolumeClaims for Postgres and Neo4j and the same memory limits as Compose.

The refresh stage runs in namespace `touchstone-phase3-v1-refresh`. `kubectl apply -k
infra/k8s/reckoner-v1/refresh` reuses the Phase 2 platform manifests unchanged through the
base `infra/k8s/platform/kustomization.yaml` (ClickHouse, collector, warehouse PVC, read
API; Dagster omitted, as in the Compose refresh profile). The smoke then scales the Reckoner
API and console to zero, exports the outbox with the collector stopped and running, runs
`refresh/refresh-job.yaml` (2560Mi), and verifies the published run. The console's dashboard
URL is `http://api.touchstone-phase3-v1-refresh:8000`.

## Measured evidence (Task 12, 2026-10-03)

Host: Apple arm64, Docker Desktop 29.8.1 with 8,319,238,144 bytes and 12 CPUs. Final Compose
smoke instance `touchstone-phase3-v1-smoke-947565`, images built from commit `5d4f808`
(`touchstone-reckoner:phase3` `sha256:35f9dce9…`, `touchstone-web:phase3` `sha256:2163e4ab…`,
`touchstone-platform:phase3` `sha256:09c63645…`). Receipts are under ignored
`artifacts/phase3/task12/compose-final-947565/`. These are fabricated-data observations of
four tasks over 37 history records; they are not capacity evidence for the simulated archive.

| Check | Result |
| --- | --- |
| Prepare | Postgres + Neo4j healthy 14.4 s; migrate 4.6 s; seed 4.7 s; graph import + GDS 8.7 s; graph evidence 7.1 s (4/4 available, PageRank not converged at 20 iterations on 8 nodes) |
| Online | up 18.2 s; workflow 6.7 s: 4/4 degraded escalations, zero provider calls; repeat run identical, no new decision; 20/20 API/web checks |
| Restart / stopped | Postgres restart to API ready 7.9 s, 20/20 checks after; with Postgres stopped 5/5 (live 200, ready 503, data 503, proxy 503, no credential in error) |
| Refresh | collector stopped: `sent 0, pending 10`; restarted: `sent 10, pending 0`; warehouse refresh 11.8 s; both tenants published (metrics stay unavailable: degraded escalations carry no outcome contributions) |
| Restore | dump restored into a new volume: all 50 table fingerprints and roles equal; graph reimported into a new volume: source nodes, relationships and deterministic projection fields equal; restored database served 20/20 checks; 29.1 s |
| Build | final Reckoner image rebuild 31 s with warm layers (first build 101 s); web 29 s and platform 25 s with existing layer cache |

Coarse memory observations (bytes; `docker stats` sample maxima are not peaks; cgroup
`memory.peak` is read at the end of each profile and covers only the container's current
start, so the online Postgres value covers only its life after the stop/start check, not the
workflow run before it):

| Profile | cgroup peaks | Largest sampled one-shot job | Max sampled combined |
| --- | --- | --- | --- |
| prepare | Neo4j 1,636,179,968; Postgres 118,861,824 | worker 362,492,723 | 1,473,217,822 |
| online | web 257,220,608; API 145,752,064; platform API 110,837,760; Postgres 39,632,896 | worker 300,102,451 | 769,948,383 |
| refresh | ClickHouse 882,495,488; platform API 88,092,672; Postgres 34,906,112; collector not readable (no shell) | exporter 246,100,787; refresh 245,576,499 | 1,488,998,889 |

Disk: the smoke's eight own volumes held 229,027,840 bytes and were removed afterwards by
`cleanup`; free disk fell from 27,758,473,216 to 27,593,035,776 bytes during the run. The
three new images add about 2.78 GB of unique image data (rounded `docker system df -v`
figures), and Docker build cache grew by about 7 GB across all Task 12 builds. The prepare
profile shut down in 3.4 s (other shutdowns were not timed); every readable cgroup reported
`oom 0` and `oom_kill 0`.

Estimates, not measurements: the profile ceilings above, and that the online and refresh
profiles fit the 8 GB allocation at full archive scale.

kind (owner-approved one-time overage to at most 26 GiB derived, hard 15 GiB free floor,
run with `--cleanup`): instance `touchstone-phase3-v1-kind-fe4ec3`, kind v0.33.0 (checksum
verified), dedicated kubeconfig, context `kind-touchstone-phase3-v1-kind-fe4ec3`, namespace
`touchstone-phase3-v1-smoke`, same images as the Compose smoke. All Jobs succeeded:

| Step | Result |
| --- | --- |
| Cluster create / image load | 9.5 s / 29.0 s for four arm64 archives |
| Stores ready | Postgres and Neo4j StatefulSets on local-path PVCs, 27.9 s (non-root UIDs 999/7474) |
| Prepare Job | migrate, seed, graph + GDS, graph evidence (4/4 available), fingerprint: 37.0 s; graph source, relationships and deterministic projection fields equal the Compose run's |
| Online | Neo4j scaled to 0; API and web ready 6.2 s; worker Job 4/4 degraded escalations, zero provider calls (12.3 s); online verification Job succeeded |
| Restart / stopped | Postgres pod replaced on its PVC, API ready again 1.4 s, online verification 20/20 with 2 cases per tenant; Postgres scaled to 0: stopped verification 5/5 |
| Final fingerprint | 50 tables; 4 decisions and 4 cases persisted across the pod replacement |

Free disk was 29,398,966,272 bytes before the cluster and fell to a minimum of
24,090,963,968 bytes at a checkpoint (an independent 15 s sampler saw 23,487,376 KiB), so
the kind node used about 5 GiB transiently and Phase 3 derived data peaked near 22.6 GiB,
below the approved 26 GiB. No checkpoint was near the floor. After the sentinel-checked
cleanup the cluster and its node volume were gone, free disk was 28,832,496 KiB (pre-kind
28,710,164 KiB), and derived data was back to 17.64 GiB. The whole node's sampled memory
maximum was 2,983,928,528 bytes; an end-of-run `crictl` snapshot showed API 130,617,344 and
web 101,011,456 working-set bytes. Two evidence defects found by this run were fixed
afterwards (stderr interleaved into stdout receipts; the first online receipt overwritten);
the job results themselves were unaffected. The kind run was not repeated.

kind rerun with the refresh stage (fix round 1, same owner bounds, `--cleanup`, guard with
`--derived-baseline-bytes 18946257796 --derived-cap-bytes 27917287424`): instance
`touchstone-phase3-v1-kind-f73b3c`, Reckoner image `sha256:0197c99b…` built from `e59e117`.
All 16 steps passed; the earlier run above stays as recorded.

| Step | Result |
| --- | --- |
| Create / load 7 images (adds platform, ClickHouse, collector) | 10.0 s / 40.6 s |
| Stores ready; prepare Job (4/4 graph evidence) | 27.6 s; 36.9 s |
| Online: workflow 4/4 degraded escalations, 0 provider calls; online check 20/20 (receipt kept); after Postgres pod replacement 20/20; Postgres stopped 5/5 | 12.4 s; 3.2 s; 1.5 s restart |
| Refresh stage in `touchstone-phase3-v1-refresh` (Reckoner API/console scaled to 0, Neo4j 0): platform core from `infra/k8s/platform` ready | 6.9 s |
| Export with collector at 0 replicas: `sent 0, pending 10`; after scale-up: `sent 10, pending 0` | 18.4 s |
| `touchstone refresh` Job: 2 declarations and 8 measurements accepted, 0 rejected, new DuckDB generation | 18.4 s |
| Verification: platform ready, workflow `reckoner` published, both tenants (2/2 tasks complete; metrics such as CPST unavailable for degraded escalations) | 3.2 s |

Disk guard (bytes): free 28,464,144,384 before the cluster, minimum 21,006,172,160 at the
prepare checkpoint (host sampler minimum 20,514,120 KiB), so derived data peaked at
26,404,230,020 bytes (24.59 GiB) against the 26 GiB cap, and free disk stayed above 19.5 GiB.
After cleanup the cluster, its node volume and its dedicated kubeconfig were gone, the volume
count was back to 32, free disk recovered to 27,757,276 KiB within a minute, and derived data
measured 18,946,474,884 bytes (17.645 GiB; baseline 18,946,257,796 plus 217 KB of receipts).
Whole-node sampled memory maximum: 2,720,861,782 bytes. End-of-run `crictl` working sets:
ClickHouse 451,751,936; read API 124,080,128; collector 29,900,800; Postgres 23,007,232.

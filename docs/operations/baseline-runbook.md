# Reckoner baseline operations

All source transactions are simulated. Fake deployment evidence and measured Anthropic evidence
are separate results. The recorded measured pilot failed its strict response-validity gate; the
1,000-case baseline was not attempted. See the [evidence](../evidence/phase-1-baseline.md).
The approved experiment is capped at $10 total provider spend with a $1 pilot sub-limit, across
both tenants and all runs. Do not reset the ledger, retry a failed pilot automatically, or alter
the frozen cohort to improve a result.

## Installed artifact and configuration

The wheel includes schemas, SQL migrations, prompt templates, default configuration, frozen lock
metadata, and build-time Git revision. The runtime hashes installed package bytes for provenance;
Git and the checkout are unnecessary at runtime. `--config` accepts either the configuration
directory or its `baseline-v1.json` file. Sibling price and tenant threshold documents are required.
Build the image with `RECKONER_BUILD_REVISION=$(git rev-parse HEAD)`; the build rejects an absent or
invalid full revision. Preflight and run must use the identical artifact and immutable settings.

CLI exit status is 0 for success, 2 for invalid input/configuration, and 3 for blocked/incomplete
execution. `--env-file /absolute/path` reads only the five supported secret keys in `.env.example`.
For a container with role-scoped environment injection, explicit `--env-file -` selects supported
process variables. Missing credentials never select fake mode. `run` without `--allow-paid`
returns before reading credentials, opening a database, or contacting a provider.

## Disposable Compose check

Start from the repository root with Docker running. The helper creates random **disposable**
database credentials in mode-600 files under a mode-700 directory; it refuses to overwrite an
existing directory and never reads the Anthropic key. The image runs as UID/GID 10001. Output is a
persistent bind mount; the smoke script assigns that directory to UID 10001 before exporting.

```bash
export RECKONER_SECRET_DIR="$PWD/artifacts/phase1-smoke/secrets"
export RECKONER_EXPORT_DIR="$PWD/artifacts/phase1-smoke/evidence"
export RECKONER_API_PORT=8006
python3 infra/smoke_credentials.py "$RECKONER_SECRET_DIR" --database reckoner_smoke_compose
bash infra/smoke-compose.sh
```

The script validates Compose quietly, starts persistent Postgres 17 with `shared_buffers=128MB`
and 40 connections, builds the image, explicitly migrates as owner and provisions separate local
login roles, then starts the API. API, Postgres, and one CLI operation each have 512 MiB limits.
CLI services use the `tools` profile and run only for the active operation. No future stack starts.
The default database is not host-published. The API binds loopback; opt-in host database access
uses `-f infra/compose.host.yaml` and loopback port `RECKONER_POSTGRES_PORT` (default 55432).

`smoke` accepts no source/config/provider selection. It seeds exactly four bundled fabricated
purchases across two tenants, uses the local fake provider, and exports runner OTLP. Evaluation,
evaluator export, and report are separate evaluator-role commands. The script verifies readiness,
actual tenant membership, both manifests, CPST `4.000066`, and restart/resume with four attempts.
That value includes synthetic token-priced usage and four assumed reviews; it is not actual spend.
Smoke requires a database name beginning `reckoner_smoke_`; the paid CLI refuses that namespace.
Use a separate project/volume/database for measured work.

The owner service receives database provisioning DSNs; runner receives only its DSN, evaluator
only its DSN, and API only its DSN. No provider key is needed for smoke. Prepared import artifacts
are mounted read-only on the owner service. The runner's read-only mount must contain only
`bundle.json`, the selected purpose's runtime JSONL, and its two cohort manifests. The API has no
data mount. Do not mount the source archive or measured oracle into runner/API containers.

After copying evidence, stop the exact smoke project; preserve volumes unless intentionally
removing this disposable test database. Never apply volume deletion to the measured project.
All Compose commands need the same exported paths; the smoke script defaults import/runtime
paths to empty subdirectories below the export directory.

```bash
export RECKONER_BUILD_REVISION="$(git rev-parse HEAD)"
export RECKONER_IMPORT_DIR="$RECKONER_EXPORT_DIR/empty-import"
export RECKONER_RUNTIME_DIR="$RECKONER_EXPORT_DIR/empty-runtime"
docker compose -p touchstone-phase1-smoke -f infra/compose.yaml stop
```

Do not print expanded `docker compose config`, full container environments, Kubernetes Secret
JSON, or secret files. `config --quiet` is sufficient.

## Separate kind check

Stop Compose first. Use the verified kind binary described in [local-runtime.md](local-runtime.md)
and a task-owned kubeconfig; never replace the user's default kubeconfig. The following assumes
`kind` is available on PATH and the already-tested image is present in Docker.

```bash
mkdir -p artifacts/phase1-kind
python3 infra/smoke_credentials.py artifacts/phase1-kind/secrets --database reckoner_smoke_kind
kind create cluster --name touchstone-phase1-smoke --config infra/kind.yaml --kubeconfig artifacts/phase1-kind/kubeconfig --wait 60s
kind load docker-image touchstone-reckoner:phase1 --name touchstone-phase1-smoke
export KUBECONFIG="$PWD/artifacts/phase1-kind/kubeconfig"
kubectl apply -f infra/k8s/namespace.yaml
for name in postgres provision smoke evaluator api; do
  kubectl -n touchstone-phase1-smoke create secret generic "$name-env" --from-env-file="artifacts/phase1-kind/secrets/$name.env"
done
kubectl apply -f infra/k8s/postgres.yaml
kubectl -n touchstone-phase1-smoke rollout status statefulset/postgres --timeout=120s
kubectl apply -f infra/k8s/migrate-job.yaml
kubectl -n touchstone-phase1-smoke wait --for=condition=complete job/reckoner-migrate --timeout=90s
kubectl apply -f infra/k8s/reckoner.yaml
kubectl -n touchstone-phase1-smoke rollout status deployment/reckoner --timeout=90s
kubectl apply -f infra/k8s/smoke-job.yaml
kubectl -n touchstone-phase1-smoke wait --for=condition=complete job/reckoner-smoke --timeout=90s
```

The smoke Job's init container has owner/runner DSNs for fixture seeding and execution; its main
container has only the evaluator DSN. Evidence is written to the `smoke-evidence` PVC with
`fsGroup: 10001`; Postgres uses a separate 1 GiB PVC. Job retries are disabled. Each application
container is limited to 512 MiB. Readiness uses `/health/ready`; API liveness uses `/health/live`.

Copy the evidence PVC out using a temporary read-only pod before deleting the cluster. A
completed Job cannot be `exec`'d; use the same image with `sleep`, UID/fsGroup 10001, no secrets,
and `smoke-evidence` mounted read-only at `/evidence`, then `kubectl cp` to ignored local storage.
Port-forward the API on loopback and run `python3 infra/verify_smoke.py LOCAL_EVIDENCE API_URL`.
Record pod limits/restarts/image IDs and kubelet stats, then terminate the forward, delete only
`touchstone-phase1-smoke`, and leave all other clusters, kubeconfigs, volumes and containers alone.

## Measured database and credentials — after whole-branch review

The human has approved the bounded experiment; whole-branch software review must finish before
accessing the root credential file, token counting, or generation. No repeated user permission is
needed within the approved constraints. Stop if data/access/price/budget gates fail.

Use a persistent **different** project, e.g. `touchstone-phase1-measured`, with a database named
`reckoner_measured`, its own generated owner/runner/evaluator/API passwords, and the same pinned
Postgres image. Persist the project volume. Do not use `smoke_credentials.py` for measured data.
Provide mode-600 `postgres.env`, `provision.env`, `runner.env`, `evaluator.env`, and `api.env` under
a protected directory outside tracked files. DSNs injected into containers use host `postgres`
and port 5432. Generate distinct `reckoner_*_login` users; `migrate --provision-roles` applies
checked migrations and grants each only its runner/evaluator/API group. API receives no owner DSN
or provider key. Empty DSNs/passwords fail role validation.

For the approved host CLI path, publish Postgres only through the loopback override. Populate the
four `RECKONER_*_DSN` entries in the existing root `.env` using host `127.0.0.1` and the published
port, with the corresponding generated local passwords. Preserve the existing
`ANTHROPIC_API_KEY` line byte-for-byte: append missing DSN entries or replace only their empty
values, never overwrite the file or print values. Keep file mode 600. Do not copy that credential
file into a worktree, image, or artifact bundle. Owner migration/import, evaluator commands, and
runner commands each open only the connection they require. The API continues using its own
separate environment file.

```bash
reckoner_source=/Users/bohdanburukhin/Projects/personal/touchstone/archive
reckoner_env=/Users/bohdanburukhin/Projects/personal/touchstone/.env
uv run --frozen --package reckoner reckoner prepare --source-dir "$reckoner_source" --output artifacts/phase1/data
uv run --frozen --package reckoner reckoner verify --source-dir "$reckoner_source" --artifact-dir artifacts/phase1/data
uv run --frozen --package reckoner reckoner migrate --env-file "$reckoner_env" --provision-roles
uv run --frozen --package reckoner reckoner import --env-file "$reckoner_env" --artifact-dir artifacts/phase1/data
```

Before preflight, record source/artifact hashes, complete coverage of all 29,757 fraud-history
rows, exact 100/900 2019 baseline and 2/18 pre-2019 pilot counts, actual tenant distributions,
unsupported records, temporal boundaries, preparation time/peak RSS and disk. Reverify the
pinned dated Haiku model and official prices; a change requires a new config and pilot. Do not
edit an existing numbered SQL migration or frozen run settings. Any mismatch stops execution.

## Pilot, baseline and evidence

Use one unique pilot ID and a separate unique baseline ID. Preflight can count provider tokens
but generates no decisions. Standard `run` requires explicit `--allow-paid`.

```bash
uv run --frozen --package reckoner reckoner preflight --env-file "$reckoner_env" --artifact-dir artifacts/phase1/data --config workloads/reckoner/config/baseline-v1.json --purpose pilot --run-id phase1-pilot-001
uv run --frozen --package reckoner reckoner run --env-file "$reckoner_env" --artifact-dir artifacts/phase1/data --config workloads/reckoner/config/baseline-v1.json --purpose pilot --run-id phase1-pilot-001 --allow-paid
uv run --frozen --package reckoner reckoner export --env-file "$reckoner_env" --run-id phase1-pilot-001 --output artifacts/phase1/pilot/runner-otlp
uv run --frozen --package reckoner reckoner evaluate --env-file "$reckoner_env" --run-id phase1-pilot-001
uv run --frozen --package reckoner reckoner export-evaluations --env-file "$reckoner_env" --run-id phase1-pilot-001 --output artifacts/phase1/pilot/evaluator-otlp
uv run --frozen --package reckoner reckoner report --env-file "$reckoner_env" --run-id phase1-pilot-001 --output artifacts/phase1/pilot/report
```

Require all 20 valid measured responses, known usage within frozen token bounds, no unresolved
charges or overages, completed runner export, and conservative remaining reservations within the
shared funds. Record pilot cost, correctness, schema validity, latency and full-run projection.
Poor but valid decisions are legitimate baseline evidence; do not tune on the frozen population.
Then repeat preflight/run/export/evaluate/export-evaluations/report with `--purpose baseline`,
`--run-id phase1-baseline-001`, and baseline output directories. A failure is not permission for
another paid run. A retry needs a new approved run ID; original costs remain in the ledger.

On interruption, reconcile uncertain attempts from evidence before continuing. Resume can
execute only never-dispatched tasks using the unchanged artifact/configuration. Never infer a
zero charge from a timeout. Known failed cases remain failed and in the fixed denominator.

Replay each manifest separately with `reckoner replay --artifact-dir EXPORT_DIRECTORY --endpoint
OTLP_HTTP_TRACES_URL`; replay never calls the model. Touchstone must ingest via OTLP only.

## Preserve and restart measured state

Budget state is in `reckoner.budget_entries`, linked to attempts, tasks and runs in the persistent
measured Postgres volume. Export a `pg_dump -Fc` backup to ignored local storage and check it with
`pg_restore --list`; record SHA-256 checksums of the dump and generated artifacts. Copy both
runner and evaluator manifests with their protobuf files. Do not export environment values.

Stop the measured Compose project without deleting its volume. To resume, use the same project
name, secret files, mounted artifacts, image and run IDs; start Postgres, verify the checksum
migration history and persisted ledger, then start the API. Unknown charges remain reserved.
Never use `down --volumes`, a fresh database, or a new project name as a recovery shortcut.


## Recorded state and restart

The 2026-09-25 run `phase1-pilot-001` has 20 failed, settled attempts costing $0.025043, with no
uncertain reservation. Both OTLP streams and the incomplete report are under
`artifacts/phase1/evidence/pilot/`. The measured services were stopped after a readable database
backup and artifact archive were saved under `artifacts/phase1/backup/`. Keep the original root
`archive/`: complete-history artifacts refer to its checksum-pinned source rather than copying it.

From this Phase 1 worktree, restore the same local deployment paths and reviewed image identity:

```bash
export RECKONER_SECRET_DIR="$PWD/artifacts/phase1/secrets"
export RECKONER_EXPORT_DIR="$PWD/artifacts/phase1/evidence"
export RECKONER_IMPORT_DIR="$PWD/artifacts/phase1/data"
export RECKONER_RUNTIME_DIR="$PWD/artifacts/phase1/runtime/pilot"
export RECKONER_BUILD_REVISION=6e1d9707e0e8bc738a1a75e56c495460700c68e3
export RECKONER_API_PORT=8008
export RECKONER_POSTGRES_PORT=55432
test "$(docker image inspect touchstone-reckoner:phase1 --format '{{.Id}}')" = 'sha256:5cf9661c1b7c8860841b88d8de7e09885d54c99d5f9de425b0e6228f3df0d6a4'
docker compose -p touchstone-phase1-measured -f infra/compose.yaml -f infra/compose.host.yaml config --quiet
docker compose -p touchstone-phase1-measured -f infra/compose.yaml -f infra/compose.host.yaml up -d --wait --no-build postgres
docker compose -p touchstone-phase1-measured -f infra/compose.yaml -f infra/compose.host.yaml run --rm --no-deps owner reckoner migrate --env-file -
docker compose -p touchstone-phase1-measured -f infra/compose.yaml -f infra/compose.host.yaml exec -T postgres psql -U postgres -d reckoner_measured -Atc "SELECT count(*), sum(actual_cost) FROM reckoner.budget_entries;"
docker compose -p touchstone-phase1-measured -f infra/compose.yaml -f infra/compose.host.yaml up -d --wait --no-build reckoner
```

The checksum-checking migration command is idempotent. The initial restored ledger must show
20 entries / 0.025043 before any newly approved experiment. Persistent volume:
`touchstone-phase1-measured_postgres-data`. Never recreate it to reset the cap. API/PG bind only
localhost8008/55432; the API has no provider key or data mount.

The recorded preflight/run used the installed config at
`/app/.venv/lib/python3.12/site-packages/reckoner/_resources/config/baseline-v1.json`, the selected
four-file runtime subset mounted at `/data`, and role-scoped container DSNs. The provider key
was read privately from the original root `.env` into the invoking process and passed by variable
name (`-e ANTHROPIC_API_KEY`) only to the temporary runner; it was not written into an artifact,
Docker command argument value, persistent service or image. The ignored operation wrapper at
`artifacts/phase1/operations/compose.py` records these local paths and rejects a changed image ID.

Restarting local services does not authorize another model experiment. The failed pilot cannot
pass the baseline gate. A reviewed remedy and explicitly approved new pilot identity must retain
all existing charges and use the remaining global/pilot funds; no repair, retry, model switch or
new run was attempted during this task. A future operator must preserve the original failed
report and both exports. Stop again with the same Compose arguments and `stop`, never volume deletion.

## Phase 1 completion revision (2026-09-26)

The preceding recorded-state section describes the original failed pilot and its
historical image. The separately approved native structured-output revision uses
`baseline-v2.json`, code `8cf7d068c2b80d3bfe362582771af5713f8f4184`, and image
`touchstone-reckoner:phase1-completion-8cf7d06` with immutable ID
`sha256:a02cd32cbae8eaf397c774b8af1276c6de96f11be1270352fb733dc07b5de327`.
The original image tag and artifacts remain intact. Current aggregate results and
backup identities are in [phase-1-completion.md](../evidence/phase-1-completion.md).

The guarded local wrapper `artifacts/phase1-completion/operations/compose.py` uses
that image via its adjacent `image.yaml` override. It reuses the original measured
project, persistent volume, credentials and source/runtime paths; all new outputs
go under `artifacts/phase1-completion/`. It asserts the image ID before every
operation and allows the provider-facing branch only for the approved
`phase1-pilot-002` and conditional `phase1-baseline-001` identities. The provider
key remains transient runner environment, never in persisted service configuration,
command-line values, images, logs or backups.

After completion the preserved ledger contains 20 original pilot calls / USD
0.025043, 20 revised pilot calls / USD 0.009168, and 1,000 baseline calls / USD
0.458940: 1,040 settled entries totaling USD 0.493151, with no unknown charges.
The measured API and Postgres stopped cleanly after backup verification.

For read-only inspection after the experiment, from the same worktree:

```bash
python3 artifacts/phase1-completion/operations/compose.py up -d --wait --no-build postgres
python3 artifacts/phase1-completion/operations/compose.py run --rm --no-deps owner reckoner migrate --env-file -
python3 artifacts/phase1-completion/operations/compose.py exec -T postgres psql -U postgres -d reckoner_measured -Atc "SELECT run_id, count(*), sum(actual_cost), count(*) FILTER (WHERE status <> 'settled') AS unsettled FROM reckoner.budget_entries GROUP BY run_id ORDER BY run_id;"
python3 artifacts/phase1-completion/operations/compose.py up -d --wait --no-build reckoner
# Stop again when inspection is finished; preserve the measured volume.
python3 artifacts/phase1-completion/operations/compose.py stop reckoner postgres
```

The wrapper's name and run-ID checks are operational safeguards, not authorization
for future provider calls. This approval covers exactly one revised pilot and one
conditional baseline; it does not authorize another run, a failed-case retry, a
ledger reset, or changing frozen settings. Export/evaluation/report replay needs
no provider credential. Read-only private evidence verification runs as image
UID 10001 with read-only evidence/script mounts, rather than weakening export file
permissions for the host account.

The completion backup uses an explicit allowlist: original prepared data/runtime
and pilot evidence, new measured evidence/operations, and versioned configuration/
prompt files. It excludes all measured/smoke secret directories, `.env` files,
kubeconfigs and credentials. Both the custom database dump and artifact archive
are fully decoded for readability and SHA-256 recorded; this check is not a
restore into a fresh database. Keep the original root `archive/` for source-backed
history and preserve both generations of evidence.

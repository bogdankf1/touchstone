# Local Touchstone platform operations

All source transactions are simulated. The measured Reckoner exports are saved OTLP bytes; local replay does not call a provider. DuckDB is the active warehouse. Snowflake configuration is an unverified example pending owner access and a spending decision.

## Compose

Use a dedicated project and a disposable ClickHouse password. Keep the Phase 1 artifacts read-only. Do not run this stack alongside the kind stack on an 8 GB Docker allocation.

```bash
export TOUCHSTONE_CH_PASSWORD='<local disposable password>'
export TOUCHSTONE_PHASE1_ARTIFACTS='<absolute path to phase-1-reckoner-v0/artifacts>'
docker compose -p touchstone-phase2 -f infra/compose.platform.yaml up -d --build --wait \
  clickhouse collector api refresh dagster dagster-daemon web replay
docker compose -p touchstone-phase2 -f infra/compose.platform.yaml port collector 4318
```

The `refresh` and `replay` containers are explicit command runners. `dagster` and `dagster-daemon` provide the orchestration UI and scheduler as separate non-root services. The UI is limited to 384 MiB; the daemon has a 2 GiB limit because Dagster's queued run launcher executes refresh workers in that container. The schedule is disabled by default (`TOUCHSTONE_SCHEDULE_ENABLED=false`). An operator can run `docker compose ... exec -T refresh touchstone refresh` or launch the Dagster asset. The warehouse lock prevents overlapping publications. Enable a conservative schedule only after validating available capacity and operations ownership.

For a fresh offline fabricated smoke, use `bash infra/smoke-platform.sh`. It creates a disposable Compose project, emits a deterministic second workflow through OTLP, checks raw ClickHouse receipts, refreshes the warehouse, queries complete and incomplete API runs, loads the web route, and removes only its own project and volumes.

For measured replay, validate each frozen manifest's SHA-256 entries, mount the saved exports as read-only, build expectation-only declarations with `touchstone declare`, then use `touchstone replay` against the collector's OTLP/HTTP endpoint. Keep pilot-001, pilot-002 and baseline as separate runs. Run `touchstone refresh` after delivery settles. `touchstone verify --expected <frozen receipt> --run-id phase1-baseline-001` is an acceptance check against the published warehouse; the receipt must never be staged as input to models. The verification fails on a changed tenant, rate, money amount, completeness state or percentile.

The read API exposes `/readyz`, `/v1/workflows`, and per-run summaries; the web service listens on port 3000. `touchstone refresh` publishes a new immutable DuckDB generation and a manifest only after dbt, MetricFlow and Elementary validation succeed. A failed refresh leaves the previous generation readable and exposes failure metadata. Restart the owned services with `docker compose -p touchstone-phase2 -f infra/compose.platform.yaml restart clickhouse collector api refresh dagster dagster-daemon web`. To shut down while preserving Phase 2 data, use `docker compose -p touchstone-phase2 -f infra/compose.platform.yaml stop`. Do not use `down --volumes` on the measured project.

Back up the `warehouse-data` volume and its manifest together while no refresh is publishing. Restoring those bytes into a disposable volume and rerunning `touchstone verify` checks the published warehouse snapshot. It does not verify raw ClickHouse or collector-queue disaster recovery; those require a separate procedure and replay plan.

## Separate kind smoke

After stopping Compose, run `bash infra/smoke-platform-kind.sh`. The script uses the checked-in exact kind binary path, a named cluster, an explicit dedicated kubeconfig and the `touchstone-phase2-smoke` namespace. It saves each locally built image with `docker image save --platform linux/arm64` and loads that archive into kind; this avoids a missing multiarch manifest digest observed with `kind load docker-image` on this arm64 host. On another host architecture, change the platform to match the kind node before running. It deploys ClickHouse, collector, refresh, Dagster UI and daemon, API and web, emits the fabricated workflow as a Job, refreshes, and checks both complete and incomplete results through the API and web. It leaves the named cluster for inspection. Delete only that cluster when finished:

```bash
/Users/bohdanburukhin/Projects/personal/touchstone/.worktrees/phase-0-foundation/artifacts/tools/kind \
  delete cluster --name touchstone-phase2-task7
```

The default script cluster name is unique to its process; set `TOUCHSTONE_KIND_CLUSTER` and `TOUCHSTONE_KIND_KUBECONFIG` to fixed dedicated names for repeatable inspection. Do not use the ambient Kubernetes context.

## Capacity and retention

The measured Compose run peaked at 2,719.06 MiB in bounded sampled service-memory totals while refresh was active, below the 8 GB Docker budget. Limits are ClickHouse 2 GiB, collector 384 MiB, standalone refresh 2.5 GiB, API/web 512 MiB each, Dagster UI 384 MiB and Dagster daemon 2 GiB. The standalone refresh runner and Dagster asset should not execute concurrently; both use one warehouse publication lock. These limits are ceilings, not simultaneous observed usage. The published warehouse volume reached 1,017 MB after seven immutable generations; prune only after recording and backing up a verified generation. See [Phase 2 evidence](../evidence/phase-2-touchstone.md) for the precise sampled scope and data receipts.

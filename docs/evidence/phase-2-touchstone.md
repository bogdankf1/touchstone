# Phase 2 local Touchstone evidence

Status: local DuckDB/ClickHouse demonstration verified on 2026-09-28. All transactions are simulated. Reckoner's saved baseline is measured provider telemetry from Phase 1; the independent second workflow uses fabricated values. No new provider call, Snowflake deployment, Jev call or production data is included.

## Frozen source and replay

The Phase 1 replay declarations now take independently pinned expected suites, required checks and metric task membership from `workloads/reckoner/expectations/phase1-replay-v1.json`. The file pins each runner/evaluator manifest hash and declares membership over frozen runner task IDs; missing evaluator checks or contributions remain expected and therefore incomplete. It contains no calculated outcome values. Rebuilding all 12 declaration sidecar files from the saved exports produced bytes identical to the sidecars already published in the measured warehouse; the SHA-256 comparison is in ignored `artifacts/phase2/receipts/task7-r1-sidecar-comparison.json`. The acceptance-only baseline JSON is tracked under `platform/tests/fixtures/` so a fresh checkout can collect the reconciliation tests, and is never copied into platform runtime staging.

The six Phase 1 OTLP manifest files and all 2,080 referenced protobuf files passed SHA-256 verification before replay. Their manifest hashes, in failed-pilot runner/evaluator, revised-pilot runner/evaluator, baseline runner/evaluator order, are:

| Export | Manifest SHA-256 | Requests | Spans | Measurement events |
|---|---|---:|---:|---:|
| Failed pilot runner | `40e10ed86cd0017d0f8f07ae69a36274bfcbcd236c5d06a0fe3702be21b07d41` | 20 | 40 | 40 |
| Failed pilot evaluator | `fd8b445b93693963e015cf7e9861cef5495dc40fdc982d61d10306b52d83c9ca` | 20 | 20 | 160 |
| Revised pilot runner | `a47c2c3c4b8b14945c3cc3d9e08cd14e681eeb351fc45e573a0776dda5b414ef` | 20 | 40 | 40 |
| Revised pilot evaluator | `5064f9665e5daa801e29ba5bc00db18a47eafe8de8fd17c290c107eee98f354f` | 20 | 20 | 160 |
| Baseline runner | `56778c8b73e9f087068e544cf6c42c09cd3c3f188b24698a8866ce8579611d8b` | 1000 | 2000 | 2000 |
| Baseline evaluator | `e809a254c5112d12fa7a00a309a08e431b174d7e79afea603c51297c16438dda` | 1000 | 1000 | 8000 |

Thus the original baseline contains exactly 3,000 spans and 10,000 measurement events before two expectation-only run declarations. Those declarations describe frozen membership, expected suites, metric identifiers and reproducibility versions; they contain no calculated result values. The saved exports were mounted read-only. The independent Phase 1 expected receipt was opened only by the acceptance verifier, never ingestion or transformations.

The first live ClickHouse replay contained pilot-001 62 spans/202 events, pilot-002 62/202 and baseline 3,002/10,002, each including two declaration receipts. After repeating the identical baseline, its raw counts became 6,004 spans/20,004 events but published logical rows and values remained unchanged. A refresh accepted 20,400 raw measurement occurrences and eight declarations, rejected zero, and retained the same six tenant/run rows. The two pilot run identities remain separate. The failed pilot is incomplete with null CPST; the revised pilot is complete with 20 completed, 17 correct and CPST `4.607480470588235294117647059` USD.

## Published reconciliation

The first measured publication was `generation-2fd62e65d1d94f35a0604d5d22f90ded.duckdb`. An acceptance container mounted its warehouse and the frozen expected receipt read-only; `touchstone verify --expected /expected.json --run-id phase1-baseline-001` returned `mismatches=[]`. A separate live API comparison checked the aggregate and each tenant on the same generation and returned zero mismatches. After duplicate replay the verifier again returned zero mismatches.

| Baseline aggregate | Published value |
|---|---:|
| Completed / correct | 1000 / 892 |
| Online model cost | USD `0.458940000000` |
| Modeled review cost | USD `96.000000000000` |
| Modeled error cost | USD `7225.337000000000` |
| CPST | USD `8.208291412556053811659192825` |
| Nearest-rank p99, completed roots | `1315.1606670003275` ms |
| False positives | 14 / 900 |
| Missed fraud | 94 / 100 |
| Escalations | 24 / 1000 |

The verifier checks exact Decimal amounts, aggregate and both tenant counts, rate numerators/denominators and numeric rates to `0.000000000001`, completeness and p99 to `0.000001` ms. Its negative tests reject a changed amount, percentile, tenant, rate or incomplete run. The strengthened verifier was rerun read-only against the retained combined measured warehouse generation and again returned `mismatches=[]`. Costs are distinct modeled amounts and measured provider usage; they are not invoice reconciliation. The high missed-fraud rate remains visible alongside 89.2% correctness.

The independent fabricated workflow emitted 44 OTLP requests across complete and intentionally incomplete runs, 22 raw spans/events each. The combined published generation `generation-7ce46141f0a449828ee72f70e56737ee.duckdb` accepted 20,440 measurement occurrences and 12 declarations with zero rejected, containing ten tenant/run rows across both workflows. The complete fabricated run has four expected, three completed, one failed, two correct and CPST USD `3.050000000000`; the missing-check run is incomplete with null CPST. These are fabricated test values, not measured provider costs.

Actual Chromium screenshots from the live combined snapshot are retained locally in ignored `artifacts/phase2/screenshots/`: `measured-baseline.png`, `failed-pilot.png`, `fabricated-complete.png`, `fabricated-incomplete.png` and `measured-baseline-narrow.png`. The measured and fabricated screens were visually inspected; the narrow viewport was also inspected. The source receipt and detailed API comparison remain in ignored `artifacts/phase2/source-checksums-verified.json` and `artifacts/phase2/api-reconciliation.json` without copying the local source exports into Git.

## Resources, recovery and limits

Review-round limits now place standalone `refresh` and `replay` behind the opt-in Compose `tools` profile; the six normal active services total 5,888 MiB of configured limits. The kind deployment likewise omits the idle standalone refresh pod. A real GraphQL UI launch on the preserved measured Compose project produced Dagster run `b07315c8-1302-467b-8ca7-1a319bbb54d5`, which reached `SUCCESS` on the execution daemon. Bounded simultaneous `docker stats --no-stream` samples of all six active containers peaked at 2,590.94 MiB summed usage; individual sampled maxima were ClickHouse 1,075.2, collector 49.46, API 86.19, UI 267.7, execution daemon 1,177.6 and web 58.95 MiB. These individual peaks may occur at different times. The full sample series and launch response are in ignored `artifacts/phase2/receipts/task7-r1-final-dagster-resource.json`. The new published generation `generation-3bc2bc238c834d8a9489e773087f3ac7.duckdb` passed read-only baseline verification with `mismatches=[]`. The measured Compose services were stopped afterward without removing their three named volumes.

Bounded `docker stats --no-stream` sampling during active measured refresh produced 130 samples. The highest sampled sum of the owned Compose services was 2,719.06 MiB; individual sampled maxima included refresh 1,051.65 MiB, ClickHouse 1,545.22 MiB, collector 244 MiB, API 154.2 MiB and web 65.55 MiB. The successful refresh window's peak sampled sum was 2,635.86 MiB. Samples do not prove exact instantaneous maxima. A later idle sample was ClickHouse 1.315 GiB, collector 58.81 MiB, API 135.1 MiB, web 93.93 MiB, refresh 19.41 MiB and replay 10.11 MiB. The collector limit rose from 256 to 384 MiB after observed pressure; refresh rose from 2 to 2.5 GiB after a real DuckDB 2 GB query-memory failure. The `mart_runs` model now extracts scalar declaration version fields before joining large event rows; existing conflict and completeness semantics remained covered by focused tests and the live reconciliation. Failed refresh logs were retained locally.

Owned volume use at sample time was ClickHouse 105 MB, collector queue 3.6 MB and warehouse 1,017 MB (seven retained immutable generations). Local application image sizes were platform 911,037,530 bytes, web 2,326,537,463 bytes and synthetic 213,643,507 bytes. Host disk had 71 GiB free. The web image size is an optimization opportunity; the measurement is recorded rather than hidden.

Restarting only the owned Phase 2 ClickHouse, collector, API, refresh and web services reopened the same combined generation. Raw run counts remained baseline 6,004/20,004, pilots 62/202 each and synthetic runs 22/22 each. A disposable restore volume copied the published warehouse snapshot and manifest read-only; the verifier returned `mismatches=[]` against the frozen baseline receipt. This verifies published warehouse restoration only. It does not establish raw ClickHouse or collector-queue disaster recovery. The disposable restore volume was removed; primary Phase 2 and sibling Phase 1 volumes and images were preserved.

## Deployment and remaining dependency

The dedicated Compose project and the separate `touchstone-phase2-task7` kind cluster use different names and were run sequentially. In kind, the fabricated Job sent 44 OTLP requests; refresh published `generation-d12c41a5764446b187adadba649282b4.duckdb` with 40 measurements, four declarations and zero rejected. The live API and web returned the expected complete CPST `3.050000000000` and incomplete null CPST. No provider credential was used.

After the review fixes, a second fresh cluster `touchstone-phase2-r1-final` validated the rebuilt lock-based platform and synthetic images. Its six deployments rolled out, the fabricated Job sent 44 requests, Dagster materialization reached `RUN_SUCCESS`, and both complete and incomplete API/web assertions passed. A live deployment query found no standalone refresh pod and summed all six container memory limits to 5,888 MiB. The dedicated cluster was deleted after saving the receipt under ignored `artifacts/phase2/receipts/touchstone-task7-r1-kind-smoke.log` and `task7-r1-kind-deployments.json`; measured Compose volumes remained preserved.

Dagster's non-root UI and daemon load the `warehouse_refresh` asset with zero enabled schedules by default. A first `dagster dev` startup failed to open the UI because its subprocesses attempted to write telemetry identity under `/.dagster` with no writable HOME. The final platform image owns `/home/touchstone`, and separate UI/daemon services became Ready. A 384 MiB daemon could not execute an asset: an explicit job exited 137 and restarted its pod. Raising the execution daemon to 2 GiB while retaining the UI at 384 MiB produced a successful asset materialization in 11.82 seconds; a 45-sample daemon cgroup series peaked at 673.17 MiB.

The real webserver GraphQL `launchRun` mutation then queued run `d369dc8b-1e79-4b13-b131-7825528a3ace`. The daemon's `QueuedRunCoordinatorDaemon` launched it, and a step worker in the daemon pod materialized `warehouse_refresh` in 11.17 seconds. GraphQL `runOrError` returned `SUCCESS`. This establishes the default UI submission and execution host without turning on the schedule. The run response, daemon logs, memory samples, failed-refresh logs and final Compose/kind receipts are retained locally in ignored `artifacts/phase2/receipts/`; the frozen manifest checksum and API receipts are in the neighboring ignored files. Actual scheduled timing/trigger execution remains disabled by design; enabling it requires an operator decision.

Snowflake remains configuration-only. The example profile uses environment references and no credentials. A live Snowflake staging, dbt/MetricFlow, dashboard and cost demonstration depends on owner access and budget approval; local DuckDB evidence does not establish Snowflake parity. The [owner dependency checklist](../operations/user-dependencies.md) tracks that gate.

## Final review fixes and verification

The final review fixed fresh-checkout dbt dependency installation, descendant
call-cost attribution, correctness eligibility, contribution compatibility and
slash-containing run IDs. Unambiguous descendant calls now roll up once to their
declared root within the same tenant, workflow and run. Orphaned, cyclic or
ambiguous ancestry is visible as unexpected task membership and withholds the
run's model-cost total; observed call/node evidence remains stored. Unknown
outcomes yield unavailable correctness, while a valid known outcome retains its
correctness when only pricing is unavailable. Contributions check their own
versions and measurement mode independently of unrelated execution/outcome gaps.
Aggregate money retains supported decimal digits; CPST still uses 28-digit
division. Fabricated costs now use the caption “Fabricated amount”. The README
records direct-wheel support and the unsupported source-distribution build path.

Focused verification covered 62 affected Python cases: the first run passed 61
and exposed an obsolete zero-correctness expectation for a missing outcome; after
updating that expectation, all six final regression cases passed. Three focused
Chromium dashboard tests passed, as did frontend type/lint/format checks and Ruff.
A separate temporary project without `dbt_packages` reproduced the fresh-checkout
failure, then passed the affected dbt-backed test after `dbt deps` installed the
unchanged lock's Elementary 0.25.1 and dbt_utils 1.4.1. Existing package directories
were preserved.

The stopped measured warehouse volume was mounted read-only to copy its current
manifest and `generation-3bc2bc238c834d8a9489e773087f3ac7.duckdb`. Source and initial
copy SHA-256 both matched
`dc765562260bddc6ccbacbb1b160f72fdc77526eba8b8b2179751b86f7b59486`.
Only the temporary copy was rebuilt with the new models, using one thread and a
2 GB DuckDB memory limit: dbt reported `PASS=51 WARN=0 ERROR=0`. The frozen receipt
verifier returned `mismatches=[]` for the baseline aggregate and both tenants,
including costs, CPST, rates and p99. The copied filename remains the historical
name; these rebuilt bytes were **not published** to the preserved measured volume.
The runbook requires refresh before serving the updated API against older
development snapshots. This fix wave did not replay exports, restart deployments,
call providers or rerun the full project suite. Detailed logs are retained under
ignored `artifacts/phase2/receipts/final-fixes/`.

# Phase 1 baseline evidence

**Status: offline software/deployment checks complete; measured experiment pending whole-branch
review.** All transactions are simulated. The checks below use four explicitly fabricated
purchases, not the CCTD baseline. No root credential file, Anthropic token-count request, paid
model request, real cohort preparation, or measured budget was used during this offline stage.

## Software and packaging

- Full frozen workspace suite: **283 passed in 217.94s**, including real PostgreSQL role,
  budget/race/recovery, telemetry, evaluation and API integration tests.
- Subsequent focused installed-package/self-review suite: **13 passed in 42.42s**. It covers the
  final strict fixture manifests, refusal of unsafe existing role memberships, independent wheel
  installation outside the checkout, safe CLI failures, no implicit paid execution, unknown
  model, incomplete exit 3, resume, exports/report and smoke/paid database isolation.
- Ruff lint passed; **63 files already formatted**; `git diff --check` and shell syntax passed.
- The CI workflow defines unit/static checks, a Postgres integration job, and image smoke after
  both test jobs. Its YAML/dependency graph, real database checks, and Compose script ran locally.
  **No hosted CI run is claimed.** The local build is arm64; amd64 CI is configured with verified
  multiarchitecture image indices but has not been executed here.

The wheel packages schemas, config, prompts, numbered SQL migrations, lock metadata and source
revision. The image executes outside the checkout and without Git. DNS/socket interception in
installed CLI tests rejects external access. Image help/provenance also ran with `--network none`.
The final image's Python/SQL/fixture bytes were compared with the checkout and matched.

Final Docker image:
`sha256:673dee1177dbc0acf3d3d339fa695d40b6e2c77ca12e1988ba0ad8a1a66ca7aa`
(arm64; 524,833,769 bytes unpacked; UID/GID 10001).

Its build source anchor is `c02ada3d2cbc993c358fd2da7501b0eedea6869a`; its installed byte digest is
`f3be03a4e8d657f1569590889b54941b4cd2146daceb73303aecd13439567f89`.
The source anchor precedes the Task 6 commit; the byte digest identifies the tested implementation
including uncommitted Task 6 changes at build time. Rebuild from the reviewed final commit before
measured operation, and keep the artifact unchanged between preflight and dispatch.

## Fabricated deployment results

The **same final image** passed Compose and a separate fresh kind cluster: explicit owner
migrations, secret-derived role provisioning, Postgres readiness, API readiness, fixed smoke,
evaluation, runner/evaluator export, and report. API receives only `RECKONER_API_DSN`; the audit
checked variable names without printing values. Postgres has persistent storage; export paths
are writable by UID 10001; runner/API receive no archive or measured oracle mount.

Both deployments produced:

| Quantity | Fabricated result |
|---|---:|
| Purchases | 4; two per tenant; 2 fraud / 2 legitimate |
| Completed / schema-valid / correct | 4 / 4 / 4 |
| Escalations | 4 |
| Synthetic token-priced model cost | $0.000264 |
| Assumed review cost | $16 |
| Business error cost | $0 |
| Synthetic CPST | $4.000066 |
| Runner protobuf requests / evaluator requests | 4 / 4 |

Every exported payload matched its manifest SHA-256. Readiness/liveness and expected transaction
membership under each tenant passed; an unknown tenant returned 404. Compose Postgres was
restarted, then smoke resumed with exactly four stored attempts. Fake evidence is labelled
`execution_mode=test` and `touchstone.provider_call_mode=fake`; it cannot satisfy the measured
pilot gate. Smoke is restricted to `reckoner_smoke_` databases, which the paid CLI refuses.

Earlier package/fixture/harness failures were fixed before final-image acceptance: missing
installed prompt/resources, absent smoke command, an inherited 100/900 report caveat, API harness
field assumptions, incomplete fixture manifests, and unsafe existing role membership. The final
strict fixture declares complete one-purchase fabricated histories and makes no CCTD history claim.

## Environment and resources

| Item | Observed value |
|---|---|
| Host / Docker architecture | Apple arm64 / aarch64 |
| Docker Engine / Compose | 29.8.0 / v5.5.1 |
| Docker CPU / RAM | 12 CPUs / 8,319,238,144 bytes (7.748 GiB) |
| Docker swap | 4,194,300 KiB configured; 0 used at sample |
| Free disk | 87 GiB before task; 86 GiB after initial builds/deployments |
| Host / image Python | 3.12.13 / 3.12.14 |
| uv / kind / kubectl | 0.11.32 / v0.33.0 / v1.36.1 |
| PostgreSQL | 17.11 |
| LiteLLM / Anthropic SDK | 1.102.1 / 0.125.0 |
| OTel SDK / Psycopg / FastAPI | 1.37.0 / 3.3.6 / 0.141.1 |
| Final Compose API sample / cgroup peak | 56.97 MiB / 74,764,288 bytes |
| Final Compose Postgres sample / cgroup peak | 19.44 MiB / 34,840,576 bytes |
| API / CLI / Postgres limits | 512 MiB each |
| Compose application restart counts | 0 (explicit Postgres restart is not a crash count) |

The pinned Postgres multiarchitecture index is
`sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f`;
registry inspection and runtime `SELECT version()` both confirmed 17.11. Its settings were
`shared_buffers=128MB`, `max_connections=40`. Memory samples/observed container peaks are bounded
smoke evidence, not maximum-memory guarantees for the real cohort or later platform stack.

Final kind API/Postgres working sets were 57,638,912 / 28,704,768 bytes; their cgroup peaks
were 65,732,608 / 104,869,888 bytes. The node sample was 882.7 MiB of 7.748 GiB. All API,
Postgres, migration, smoke and evaluator containers had zero restarts. API requested 100m CPU /
64 MiB; Postgres and Jobs requested 100m / 128 MiB; all application limits were 512 MiB.
Kind imported the final Docker image as
`sha256:74a932aaa5cb53cd8d823f40535ee9e575d634fcbdbf03d6e2ac8d9ce683a364`,
with that ID observed on API, migration, smoke and evaluator containers.

Final local evidence is under `artifacts/phase1-smoke/final-evidence/` and
`artifacts/phase1-kind/final-evidence/`, with checksums in
`artifacts/phase1-smoke/final-checksums.json`. Pod resource/image receipts are in the kind artifact
directory. After copying and checking these outputs, only the owned kind cluster was deleted;
final Compose containers were stopped with their persistent test volume retained. The task-only
integration database container was removed. Unrelated volumes/clusters and user kubeconfig were
preserved.

## Architecture and remaining measurement gates

The Structurizr model and Mermaid flow distinguish current baseline software from the planned
Touchstone collector/storage/dashboard and Reckoner cascade/graph. An eight-node archify drift
candidate passed all **nine** artifact checks, with **zero composition errors/warnings**. Local
source paths were checked against code. This was source/drift and deterministic validation only;
no HTML delivery, browser evidence, or perceptual visual review is claimed.

Next, after controller whole-branch review, use the [runbook](../operations/baseline-runbook.md)
to prepare/verify CCTD, establish isolated persistent measured Postgres state, reverify provider
availability/prices, and preflight the approved $1 pilot inside the shared $10 cap. Exact
29,757-fraud history coverage, frozen 100/900 and 2/18 counts, source/artifact hashes, real tenant
distributions, preparation resource use, pilot metrics/projection, and full baseline results are
**not yet measured**. No fake result substitutes for any of these acceptance gates.

# Phase 1 baseline evidence

**Status: software, deployment and actual source preparation verified; measured pilot failed its
validity gate; 1,000-case baseline not attempted.** All transaction data is simulated. The pilot
made 20 real Anthropic calls, with $0.025043 calculated from provider-reported usage. It produced
zero valid decisions. There was no retry, response repair, model/config change, or budget reset.

## Reviewed software and artifact

Final controller verification against separate disposable PostgreSQL: **299 passed in 262.44s**;
Ruff passed, **63 files formatted**, and `git diff --check` passed. Earlier Task 6 installed-wheel
checks exercised the CLI outside the checkout, missing resources/credentials, blocked paid mode,
unknown model, incomplete exit 3, fixture isolation, role provisioning, exports and resume.
CI defines required unit/static, real-Postgres integration and image-smoke jobs. These checks ran
locally; **no hosted CI or local amd64 execution is claimed**. The tested local image is arm64.

Frozen reviewed source: `6e1d9707e0e8bc738a1a75e56c495460700c68e3`.

- Docker image: `sha256:5cf9661c1b7c8860841b88d8de7e09885d54c99d5f9de425b0e6228f3df0d6a4`
- Installed byte digest: `6df4e25c9297f29d102ec153a13d3e16bef9d91f06172ecb8ecde58cdefa3066`
- Image size: 524,831,581 bytes; UID/GID 10001.
- Config: `46cd2f3e601dc83d72aa325c349ccd10c6feb3e3798387e32f7bf3f89aac4b3e`
- Prompt: `ede8d4931914968eeb0ab32ceef0daebbeaaa78087d45de02b2f6ed21a1f4c12`
- Price table: `245d2d3dd12226e11e1328b65a7a63eb2db964b9e40a71eb56a0f30d07e6bcc2`

The wheel includes schemas, checked SQL migrations, templates, default config, frozen lock
metadata and build revision. Runtime needs neither Git nor a checkout. The same reviewed image
served the API and every preparation, import, preflight, run, export and evaluation operation.
No executing source, model, prompt, configuration, price or bundle changed during the pilot.

## Fresh fabricated deployment checks

After whole-branch fixes and clean review, **fresh Compose and fresh kind databases** exercised
that same image. Both completed owner migration/provisioning, API readiness, four fabricated
purchases, evaluation, both exports and strict report. Each produced 4/4 valid/correct escalations,
synthetic model cost $0.000264 and synthetic CPST $4.000066, including $16 assumed review cost.
These are fixture results, not measured CCTD performance or actual provider spend.

Both exports passed checksums and decoding: **12 spans / 40 measurement events per deployment**,
consistent `provider_call_mode=fake`, `simulated=true`, `dataset_simulated=true`, and four response
hash references. API liveness/readiness and exact tenant membership passed; unknown tenant was
404. Compose PostgreSQL restart/resume retained exactly four attempts. No application crash
restart occurred. Smoke cannot run in the measured database or satisfy the paid pilot gate.

kind imported the image as
`sha256:cee74e0251448710f5a85affecce1f7847ac555033d4ae61fd34c0e88cc84a24`;
API, migration, smoke and evaluator used it, and installed provenance queried inside kind matched
Compose exactly. Evidence was copied from its PVC before deleting only the owned cluster.
Reviewed smoke files remain under `artifacts/phase1-reviewed-smoke/evidence/` and
`artifacts/phase1-reviewed-kind/evidence/`. Previous pre-review smoke evidence remains separate.

## Actual simulated-source preparation

Preparation, one explicit verification and owner import all returned bundle
`2dcc4a9838db77552872ea489ff8a8e3b13a84b37a5806b2b02291c9f23848e0`.
Source and generated-file SHA-256 values are recorded in the aggregate
[checksum receipt](phase-1-data-checksums.json). Original source files were read-only and unchanged.

| Source disposition | Count |
|---|---:|
| Total transaction rows | 24,386,900 |
| Eligible positive purchases | 23,122,004 |
| Unsupported nonpositive amounts | 1,264,896 |
| Invalid / unassigned | 0 / 0 |
| Source fraud / legitimate | 29,757 / 24,357,143 |
| Pre-2019 / 2019 / 2020 onward, all dispositions | 22,326,462 / 1,723,938 / 336,500 |
| Assigned users | 2,000 |
| Complete retained histories | 1,441 users / 23,121,349 source-backed rows |
| Fraud rows covered by retained histories | **29,757 / 29,757** |

Complete histories remain in the checksum-pinned archive; the compact prepared artifacts store
backing references rather than duplicating the full source. Retain the archive with these
artifacts. No graph was constructed. USD and UTC remain explicitly documented assumptions.

| Frozen cohort | Tenant A fraud / legitimate | Tenant B fraud / legitimate | Total |
|---|---:|---:|---:|
| Pre-2019 pilot | 1 / 12 | 1 / 6 | 2 fraud / 18 legitimate |
| 2019 baseline | 61 / 651 | 39 / 249 | 100 fraud / 900 legitimate |

Seed: 20260925. Actual pilot timestamps range from 1999-05-23T17:40:00Z to
2018-11-28T08:53:00Z; baseline from 2019-01-01T10:16:00Z to 2019-12-30T19:18:00Z.
2020 onward was reserved and unused; its observed lack of positive fraud cases does not support
an independent stratified confirmation cohort. Tenant assignment uses pre-holdout history;
labels, tenant assignment, raw payment credentials and histories are excluded from model inputs.
The runtime mount contains only the bundle index, selected runtime JSONL and two tenant manifests.

Preparation took **926.11 seconds** at one CPU with a 512 MiB container limit. Observed process
high-water RSS was **347,520 KiB (339.375 MiB)**, sampled every two seconds; this is an observed
peak, not a guarantee of the exact instantaneous maximum. Cgroup peak reached its 512 MiB limit
including reclaimable file cache, with no OOM. The host `time` RSS measures the Docker client and
is not substituted for container process RSS. Prepared data occupied 2.1 MiB and runtime subsets
1.2 MiB; disk had 85 GiB free before preparation and 86 GiB afterward.

## Measured pilot and failed gate

On 2026-09-25 the official [model list](https://platform.claude.com/docs/en/models/overview)
listed `claude-haiku-4-5-20251001`; [standard pricing](https://platform.claude.com/docs/en/about-claude/pricing)
remained **$1/M input and $5/M output tokens**, matching the frozen price document. Actual count
and generation requests subsequently established access to this exact model. Invoice
reconciliation was not performed.

Two delegated preflight tool submissions were rejected before process creation because automatic
review could not recognize the original user's approval in delegated context. The controller
submitted the **unchanged operation** through the same review with the original trusted approval
messages; review accepted it. The controller executed provider-facing commands. This introduced
no provider retry, alternate route, code change or new run ID.

Unique run `phase1-pilot-001` used the unchanged reviewed image/config/bundle. Twenty token counts
succeeded, with estimates 235–245 tokens and aggregate maximum reservation **$0.068576**. Generation
ran from 17:20:27 to 17:21:25 UTC. The runner had already exited 3 when the controller attempted
to act on the failure alert; all twenty calls occurred. The run was **not interrupted**.

| Pilot result | Observed value |
|---|---:|
| Expected / attempted / failed | 20 / 20 / 20 |
| Completed / schema-valid / required-suite pass | 0 / 0 / 0 |
| Known input / output tokens | 4,783 / 4,052 |
| Token-priced provider cost | **$0.025043** |
| Unsettled reservations / unknown costs / overages | 0 / 0 / 0 |
| Remaining shared $10 / pilot $1 funds | $9.974957 / $0.974957 |
| Normal finish / output-limit finish | 16 / 4 |
| Failed-call latency mean / nearest-rank p99 | 2,867.755 ms / 3,626.140 ms |
| Successful-task latency population / p99 | 0 / unavailable |
| CPST | **Unavailable: incomplete outcomes** |

All twenty stored responses began with a closed Markdown fence containing an outcome-only JSON
object, followed by extra prose. Consequently none was valid as the required whole-response JSON;
four also hit the output limit. All persisted requests contained the correct allowlisted facts
and required only JSON; all reported the pinned model. These observations support **model format
noncompliance**, not accepting or repairing the responses after the fact. Raw content was retained
only in private operational state; diagnosis exposed counts/format categories, never prompts,
transaction records or response prose. All twenty canonical response hashes matched stored bytes;
known usage and money remained within every frozen bound.

Evaluation retained all twenty cases and recorded twenty missing-outcome errors, returning 3.
The strict report also returned 3 with `incomplete_outcomes` and null CPST. Its zero completion,
correctness and schema-validity numerators are over the fixed twenty-case denominator. Zero
recorded decision-based error rates do not establish useful quality: there were no valid decisions.
The arithmetic cost of 1,000 equally priced failed calls would be $1.25215; that is **not a valid
baseline forecast or a passed gate**. No full-cohort preflight or baseline generation was attempted.

Runner and evaluator exports each contain twenty checksummed OTLP requests. Decoding verified
**60 spans / 200 measurement events** with `provider_call_mode=measured`, `simulated=false` and
`dataset_simulated=true`. The report links twenty response hashes. Both streams remain separate
for later OTLP replay; no Touchstone collector or warehouse was introduced.

## Environment, preservation and remaining work

Docker Engine 29.8.0 / Compose 5.5.1; 12 CPUs; 8,319,238,144-byte Docker RAM (7.748 GiB);
4,194,300 KiB swap configured. Python host/image 3.12.13/3.12.14; uv 0.11.32; kind 0.33.0;
kubectl 1.36.1; PostgreSQL 17.11; LiteLLM 1.102.1; Anthropic SDK 0.125.0; OTel SDK 1.37.0;
Psycopg 3.3.6; FastAPI 0.141.1. API/CLI/Postgres limits are 512 MiB each. PostgreSQL uses
`shared_buffers=128MB`, `max_connections=40` and pinned multiarchitecture image
`sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f`.

Reviewed Compose API/Postgres memory samples were 57.52/19.94 MiB; kind working sets were
58,789,888/26,660,864 bytes, with zero restarts. Measured API/Postgres initial samples were
55.07/22.85 MiB. These samples do not size future platform workloads.

The measured project is `touchstone-phase1-measured`, database `reckoner_measured`, persistent
volume `touchstone-phase1-measured_postgres-data`; API/PG loopback ports are 8008/55432. Separate
runner/evaluator/API login roles have no privileged flags. Protected local secrets remain under
`artifacts/phase1/secrets/`; the root `.env` has four local DSNs and retains its original Anthropic
key line byte-for-byte, mode 600. The key was never copied into artifact files or the image.

Backups were verified **before** stopping API/Postgres, which both exited 0. The volume and all
20 settled ledger entries remain. Backup location: `artifacts/phase1/backup/`.

- Database custom dump SHA-256: `2b24dd1c7a5db16abb9f021e0afe9b4bba26e12a9a6195fa3d3b7f409c344e1a`.
  `pg_restore --list` and full `pg_restore --file /dev/null` both passed.
- Artifact archive SHA-256: `c5344e9c7d5cb7972880db87b33afb7779dba468e5677d2671b1898a7edb1f90`.
  Listing and full extraction-to-null passed; 68 file checksums are recorded locally.
- Prepared data, runtime subsets, report, manifests, protobufs and operation receipts are ignored
  local artifacts. Provider credentials and source archive are excluded from the backup archive.

The [runbook](../operations/baseline-runbook.md#recorded-state-and-restart) gives the restart path.
A future experiment needs a reviewed response-contract remedy and an explicitly approved new
pilot/run identity while retaining this $0.025043 charge. Do not reuse the failed run to replace
outcomes or reset the ledger. Baseline acceptance remains incomplete.

The current/planned Structurizr and Mermaid boundaries remain accurate. The bounded archify
source/drift candidate passed 9/9 deterministic checks with zero composition errors/warnings.
No HTML delivery, browser execution or perceptual review is claimed. No Phase 2 work, push or
merge was performed.

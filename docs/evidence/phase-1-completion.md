# Phase 1 completion experiment — native structured output

All transaction data is simulated CCTD. These are local experimental measurements,
not production deployment or real customer results. The 10% fraud evaluation cohort
is deliberately enriched; its rates and costs are not population estimates.

The baseline completed all 1,000 cases with valid outcomes and known model cost
USD 0.458940. It missed 94 of 100 fraud cases: the measured result establishes a
weak naive baseline for later comparison, despite 89.2% overall correctness.

## Scope and frozen artifacts

The approved remedy adds Anthropic native JSON-schema output to the same dated
Haiku 4.5 model. The outcome-only schema, strict local parser, no retries/fallback,
no tools or prompt caching, temperature 0, 256-token output ceiling, cohort and
pricing remain frozen. Grammar compilation caching is a provider implementation
detail and is distinct from disabled prompt/token caching. No decision-quality
prompt tuning was performed.

The original `phase1-pilot-001` remains failed: 20 invalid responses and USD
0.025043 in settled charges. Its image, configuration, report, exports and database
entries were preserved. See [the original evidence](phase-1-baseline.md).

| Artifact | Identity |
|---|---|
| Reviewed source | `8cf7d068c2b80d3bfe362582771af5713f8f4184` |
| Installed provenance | `8cf7d068c2b80d3bfe362582771af5713f8f4184:f10ad81907e79e708b9ec900b850fd13bb5d212ed3e3145ec30a5078ea8672e0` |
| Image | `sha256:a02cd32cbae8eaf397c774b8af1276c6de96f11be1270352fb733dc07b5de327` |
| Image tag | `touchstone-reckoner:phase1-completion-8cf7d06` |
| Configuration | `6b0a2ed47cef742ec4225a835358ce4ac4f043b03d993d32bcc6e4fcf68b7afb` (`baseline-v2.json`) |
| Prompt/request construction | `b235b57fc482f455e5658beaa78683b740416f8c788ab2024253c404c509fc1e` |
| Price table | `245d2d3dd12226e11e1328b65a7a63eb2db964b9e40a71eb56a0f30d07e6bcc2` |
| Frozen bundle | `2dcc4a9838db77552872ea489ff8a8e3b13a84b37a5806b2b02291c9f23848e0` |
| Model | `anthropic/claude-haiku-4-5-20251001` |

Source/bundle checksums were verified again using the installed image with network
disabled and read-only mounts; the source was not re-extracted. Runtime mounts
contain only the bundle, selected unlabeled runtime cohort and two tenant manifests,
byte-identical to the prepared artifacts. The runner has no oracle access;
evaluation uses the isolated evaluator role. Owner import retains its separate
oracle-loading permission. Amounts use the approved USD and UTC assumptions.

[Official pricing](https://platform.claude.com/docs/en/about-claude/pricing) and the
[model list](https://platform.claude.com/docs/en/models/overview) were rechecked on
2026-09-26: base input USD 1/million tokens and output USD 5/million tokens. The
[structured-output documentation](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
was checked for native schema support and refusal/truncation caveats.

## Validation before measurement

The reviewed code passed 312 tests against isolated pinned Postgres, plus lint and
format checks. Native count and generation HTTP payloads were tested with captured
transport. Local installed-image deployment checks then passed in sequence:

- A new disposable Compose project: four fabricated cases, exact CPST USD 4.000066,
  four attempts after restart/resume, tenant isolation, readiness, private export
  verification, 12 spans, 40 measurement events and four response hash references.
- A separate task-owned kind cluster loaded the same image and passed the same
  four-case/report/export/readiness checks, with zero application/database/job
  restarts. Its containerd manifest was
  `5f144315c9d975c77b3f0ea733c17dc15911e6c5eede8107d5d675605449f896`.

Both disposable environments were removed after preserving their evidence; the
measured database was stopped during these checks. Docker 29.8.0 had 8,319,238,144
bytes available memory; the host had about 93 GiB free afterward. The arm64 image
runs as UID/GID 10001 and occupies 524,840,116 bytes. Compose samples showed API
56.39 MiB and Postgres 19.84 MiB, each limited to 512 MiB. These snapshots do not
prove future-stack capacity. Hosted CI and amd64 execution are not claimed here.

The archify drift check passed all nine showcase artifact checks, with zero errors
or warnings. The nine implementation seams and Structurizr model still match the
existing topology; native schema handling stays inside the provider boundary.
This is source/layout validation, not delivered HTML, browser or perceptual review.

## Revised pilot

`phase1-pilot-002` used the frozen 20 pre-2019 cases: two fraud and 18 legitimate,
tenant-a 13 and tenant-b seven. The provider accepted native-schema token counting;
input estimates were 405–415 tokens. The conservative reservation was USD 0.073016,
within the cumulative USD 1 pilot limit after the historical USD 0.025043 charge.

The one approved pilot completed all 20 calls and accepted all 20 responses. All
charges were known, with no monetary/token-bound overages or uncertainty. Model
cost was USD 0.009168. Its 20 response hashes and both 20-request OTLP manifests
were verified; 60 spans and 200 events retained `provider_call_mode=measured`,
measurement `simulated=false`, and `dataset_simulated=true`.

| Metric | Aggregate | tenant-a | tenant-b |
|---|---:|---:|---:|
| Valid/completed | 20/20 | 13/13 | 7/7 |
| Correct, including escalation | 17/20 | 10/13 | 7/7 |
| Escalated | 3/20 | 2/13 | 1/7 |
| False positives / legitimate | 2/18 | 2/12 | 0/6 |
| Missed fraud / fraud | 1/2 | 1/1 | 0/1 |
| Model cost, USD | 0.009168 | 0.005962 | 0.003206 |
| Review cost, USD | 12 | 8 | 4 |
| Error cost, USD | 66.318 | 66.318 | 0 |
| CPST, USD (rounded) | 4.60748047 | 7.4323962 | 0.57188657 |
| Successful-task p99, ms | 1697.904167 | 1697.904167 | 1010.447917 |
| Latency population | 20 | 13 | 7 |

The required correctness suite passed 17/20 cases (85%) and therefore has status
`fail`. This is honest naive-baseline quality evidence. The approved progression
gate requires valid, completed, known-cost outcomes and affordability; it does not
require all decisions to be correct. No outputs or historical failures were repaired.
Pilot cost extrapolated to 1,000 cases is USD 0.4584; it is a projection, not the
baseline cost. The actual full-cohort reservation must independently fit remaining
funds. The ledger after this pilot contains 40 settled calls costing USD 0.034211.

## Baseline and preservation

`phase1-baseline-001` used the original 1,000 2019 cases: 100 fraud and 900
legitimate, tenant-a 712 and tenant-b 288. Native-schema preflight counted inputs
at 404–417 tokens. The full conservative reservation was USD 3.649471; together
with USD 0.034211 already settled, it left USD 6.316318 unreserved under the
unchanged USD 10 lifetime limit. Both tenant run records matched the pilot's
frozen configuration and bundle before generation.

Exactly one baseline generation run completed 1,000/1,000 outcomes, with zero
failed, pending, uncertain or evaluation-error cases. The evaluator processed all
1,000; no model retries, fallback, quality tuning or cohort changes occurred.

| Metric | Aggregate | tenant-a | tenant-b |
|---|---:|---:|---:|
| Valid/completed | 1000/1000 | 712/712 | 288/288 |
| Correct, including escalation | 892/1000 | 644/712 | 248/288 |
| Escalated | 24/1000 | 16/712 | 8/288 |
| False positives / legitimate | 14/900 | 10/651 | 4/249 |
| Missed fraud / fraud | 94/100 | 58/61 | 36/39 |
| Model cost, USD | 0.458940 | 0.326795 | 0.132145 |
| Review cost, USD | 96 | 64 | 32 |
| Missed-fraud loss, USD | 6915.68 | 4521.48 | 2394.20 |
| False-positive margin loss, USD | 309.657 | 237.609 | 72.048 |
| Total modeled cost, USD | 7321.795940 | 4823.415795 | 2498.380145 |
| CPST, USD (rounded) | 8.20829141 | 7.48977608 | 10.07411349 |
| Successful-task p99, ms | 1315.160667 | 1404.176959 | 1210.695458 |
| Latency population | 1000 | 712 | 288 |

CPST is exactly `(0.458940 + 96 + 6915.68 + 309.657) / 892`, represented by
the report as `8.208291412556053811659192825` USD. Per-tenant CPST is respectively
`4823.415795 / 644` and `2498.380145 / 248`; it is not averaged to form aggregate
CPST. All counts, costs and rates reconcile across tenants using decimal arithmetic.
The required correctness suite reports `fail`, passing 892/1,000 (89.2%); the
schema-validity and completion rates are both 100%. There are 959 auto-approvals,
17 auto-declines and 24 escalations. Of the 100 fraud cases, 94 were approved,
three declined and three escalated.

Only the model column is actual provider spend. Review cost is the approved USD 4
per escalation assumption; missed-fraud loss uses the simulated transaction amount,
and false-positive loss uses the approved 30% margin assumption. The USD 7,321.795940
modeled total is not a provider bill or observed real-world loss. Escalations count
as correct but costly. The fraud-enriched cohort, oracle-label evaluation and lack
of routing/history/graph signals limit conclusions about real-world performance.

Latency uses the report's successful-task population and nearest-rank p99
(sorted observation at `ceil(0.99 * N)`). The table rounds milliseconds to six
places; the aggregate report value is `1315.1606670003275` ms. Percentiles are
computed separately per population, so aggregate p99 can be lower than one tenant's
p99. No incomplete calls were excluded in this completed baseline; original failed
pilot calls remain in their own fixed denominator and in the lifetime cost ledger.

Known usage was 409,060 input and 9,976 output tokens, yielding USD 0.458940 at
frozen prices. All 1,000 canonical response hashes and token/money bounds passed.
Both OTLP streams contain 1,000 checked requests: 3,000 spans and 10,000 measurement
events in total, with measured-call and simulated-dataset flags preserved. The
platform has not yet ingested these exports; no collector/warehouse/dashboard
completion is implied. Deterministic reports and exports are retained locally at
`artifacts/phase1-completion/evidence/{pilot,baseline}/`.

A bounded resource sample during baseline generation showed the runner at
257.6 MiB, API at 61.37 MiB and Postgres at 38.55 MiB, each under its 512 MiB limit.
These are point samples, not peak-memory measurements.

## Final ledger, backups and stop

| Run | Settled calls | Provider cost, USD |
|---|---:|---:|
| Original failed pilot `phase1-pilot-001` | 20 | 0.025043 |
| Revised pilot `phase1-pilot-002` | 20 | 0.009168 |
| Baseline `phase1-baseline-001` | 1000 | 0.458940 |
| Lifetime total | 1040 | 0.493151 |

All entries are settled, with zero unknown charges, outstanding reservations or
overages. The cumulative pilot spend is USD 0.034211 of USD 1; remaining lifetime
funds are USD 9.506849 of the unchanged USD 10 cap. No extra paid run is authorized
by this remaining balance.

The database custom dump and artifact archive were saved under
`artifacts/phase1-completion/backup/`. `pg_restore --list` and complete decoding to
`/dev/null` passed; the artifact archive listing and complete extraction to
`/dev/null` passed. These are readability checks, not a fresh database restore.
The explicit archive allowlist excludes every measured/smoke secret directory,
`.env` file and kubeconfig; member names were checked before acceptance.

- Database dump SHA-256: `c70155f472f09b693011e8ae76800e04e6fd4903906edab3e72ff598cf0c42c5`.
- Artifact archive SHA-256: `1b629b256438ca2f0e0ff1111de29221c1fbb02e31754d9ded0f03bb48b6c5a9`.
- The checksum receipt records 2,052 files. Original prepared data, runtime subsets
  and failed-pilot evidence are included without replacing the prior backup.

Measured API and Postgres then exited cleanly with code 0. Volume
`touchstone-phase1-measured_postgres-data` remains intact with all 1,040 ledger
entries. The original image, v1 configuration, prompt and failed-pilot report were
verified unchanged. No merge, push, Phase 2 services or further provider call was
performed. Restart/inspection commands are in the [runbook](../operations/baseline-runbook.md).

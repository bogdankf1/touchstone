# Reckoner v1 bounded retrieval benchmark

The source is simulated CCTD. This benchmark covers all seven frozen validation
transactions on 2018-06-01 and the full retained 1,441-user entity manifest. It
is not evidence of full validation, 2019 operational coverage, production traffic,
fraud utility, or improved decisions. The 200-case paired model experiment and
live baseline/current comparison remain pending Task 13 authorization and execution.

## Frozen population and preparation

`artifacts/phase3/task11/protocol.json` freezes the seven canonical query documents,
cutoffs, per-query candidate IDs, union membership, and schedule before timing.
Protocol ID: `4116172184cce022379cc463421031a1d96a0e3e203270c44cb0a7b95befbf49`.
The complete candidate union contains **381,129 transactions**, hash
`4fa23c77217823b2d630d57fd02e7a99388e9486ceab26c633fdb3f3d16ca03c`.
Per-query eligible counts in protocol order are 116000, 262393, 262453, 262393,
116007, 262432, 262397. Nonpositive unsupported counts are 5788, 13829, 13843,
13828, 5786, 13838, 13830. Populations overlap; do not sum them.

Candidates are same-tenant positive supported purchases occurring strictly before
the query, resolved strictly before the query, and resolved at or after query minus
90 days. Resolution is the explicitly simulated seven-day policy. All source
histories and resolutions, including unsupported amounts, remain retained.

Every vector uses its own candidate timestamp, complete source-backed preceding
30-day card history, and the previous card transaction from full retained history.
Equal timestamps establish no order. Preparation reads each complete card window
once; neither source labels nor tenant identities are vector coordinates. The
unchanged scaler was fitted on all 2,000 development members, including the pilot
subset, with missing coordinates treated individually. Scaler ID:
`a5fd8dbaa782b6c5cf66e947f6ba13faca07dea5be2fefc231da939949e10dbb`.
All 381129 vectors were persisted in 130.1365 seconds. Stored count and sorted
membership hash match the declaration, and all seven strict SQL coverage checks
pass. These are preparation observations, not query timings.

## Three separate questions

1. Exact neighbourhood equivalence: SQL and hand-written Cypher fully materialize
   the same canonical transactions and shared-merchant identities, with the same
   strict 30-day cutoff and declared shared scope. Coverage checks run in both
   adapters. No vector search, feature calculation, GDS lookup, or provider work
   is hidden inside this timed operation. Full rows are reconciled, not display
   truncations. Existing SQL indexes and function EXPLAIN are recorded.
2. Comparable-case retrieval: full eligible SQL/Cypher ID sets are reconciled
   separately. pgvector exact Euclidean top five uses all eligible vectors, with
   transaction-ID tie-breaking. Top-five IDs, distances, empty/unavailable status,
   and per-query label agreement are reported. Labels are joined only by the
   privileged report evaluator after retrieval; agreement is only a relevance
   diagnostic on an enriched simulated cohort.
3. Added GDS evidence: unavailable without identical persisted scoring evidence or
   separately approved new provider calls. No provider call, synthetic prediction,
   CPST improvement, or decision-impact estimate substitutes for that experiment.
   The prior frozen PageRank failed to converge at 20 iterations; Louvain includes
   isolates. Neither community membership nor centrality demonstrates fraud utility.

## Timing and resource interpretation

The declared schedule contains a first pass after both database processes restart,
then five warm repetitions, alternating SQL/Cypher execution order and retaining
all seven queries in frozen order. Timings use monotonic elapsed client time and
include complete result consumption. Connection handshakes are outside the timed
region. This is a database-process restart protocol; **OS caches are uncontrolled**.
The first original pass briefly overlapped an API test and is retained as invalid;
one replacement first-after-restart pass is recorded separately after tests.
Warm measurements are retained rather than rerun to select favorable results.

Top-five timings are separate from neighbourhood timings and occur after exact-set
reconciliation: their first observations are not called cold. Nearest-rank p50,
p95 and p99 carry population counts. Seven first observations and 35 warm
observations cannot establish a service-level tail or general graph speedup.
This compares the shipped SQL/Cypher implementations on this bounded workload;
it is not a database-engine ranking. No indexes or algorithm parameters were tuned
after inspecting outcomes.

The preparation and graph profile uses the pinned Task3 images and existing
preserved volumes, 2 GiB Postgres and 4 GiB Neo4j caps, within the 8 GB RAM/4 GB swap
host budget. Neo4j was stopped during vector preparation. Task11 fabricated store
fixtures use separate containers and volumes and are stopped during measurement.
Resource samples are observations, not exact peaks; cgroup memory peaks cover their current container-start lifetime,
not an individual query. Earlier Task3 peaks remain in their original receipts. Final disk accounting includes artifacts and preserved
Phase3 stores; the 20 GiB derived cap and 15 GiB free floor are checked explicitly.
Source preparation, old receipts, graph projections, and algorithm parameters are
unchanged. All measured and fixture volumes are preserved.

## Interfaces and publication

- `benchmark_queries(cases, relational, graph)` records six complete passes and
  separates exact neighbourhood membership from eligible candidate agreement.
  `SQLQueries` and `CypherQueries` expose the equal-work store operations.
- `candidate_vectors(candidates, history, scaler)` requires the complete declared
  candidate population and a full source-backed history reader.
- `compare_arms(results, expected_ids)` keeps treatments separate and rejects
  calibration transfer across model-input arms. Its calibration binding is model,
  prompt, question, evidence and retrieval window, identical to the generic
  platform binding: a changed retrieval window changes model input, so it needs
  its own calibration. `comparison_eligibility` suppresses deltas for incomplete
  or incompatible membership, provenance, costs or refresh. Exact eligible-set
  agreement compares SQL/Cypher candidate IDs as sets and reports duplicates;
  any duplicate blocks agreement.
- `reckoner v1 compare --arms ARMS.json --expected IDS.json --output PREFIX
  --measurement-mode measured-comparison|synthetic-fixture` writes immutable,
  versioned JSON and a comparison Markdown (arms, eligibility, reasons, CPST delta
  or `unavailable`) without dispatching inference. CPST deltas are computed exactly
  (76-digit decimal context) in both the workload and the platform. An arm without
  a calibration ID is reported as missing provenance, not as calibration reuse. A
  parity test keeps the workload and platform invariants, pins and calibration
  bindings in correspondence (`cohort_id`/`cohort_version`,
  `oracle_version`/`reference_version`, `execution_mode`/`measurement_mode`).
- `write_report` refuses a retrieval or comparison report without an explicit
  `measurement_mode`; measured and synthetic reports receive different headings
  and labels. `PREFIX` keeps its whole name (`report.v2` writes `report.v2.json`).
  Code fences are longer than any backtick run in the content. Both files are
  written to temporary names in the target directory and hard-linked into place;
  an existing file is never replaced, and a failed second link removes the first,
  so a failure leaves no partial pair.
- Generic `run-declaration-v1.comparison` is optional. Old declarations remain
  valid and have unavailable comparison provenance. It declares reference/version,
  business-cost identity and model/input/evidence arm versions. `/v1/comparisons`
  reads both summaries from one immutable warehouse generation. Cross-tenant
  summaries sum governed numerators and denominators; excluded tenants block
  comparison. Synthetic workflows are never pooled with Reckoner.

The dashboard permits an explicit current-run selection, displays both arm
provenance records, and hides deltas when eligibility fails or generations change.
A comparison API failure is shown as an error, not as "no compatible run". The
simulated label appears when either arm is fabricated or on simulated data.
Its fabricated comparison fixture proves UI plumbing only. Task 13 must produce
complete original declarations and real measured results before any live v1 delta
can be shown. Failed refresh state remains distinct from a published prior snapshot.

## Legacy baseline comparison attestation

The immutable original baseline declarations pin exact tenant/case membership,
cohort, configuration, dataset and code identities. Their original measurement
reproducibility pins model and prompt versions; governed call records supply
actual call identities and currency. They do not explicitly state the reference
label policy, business-cost subset identity, or applicability of retrieval,
question and calibration policies. Never alter those original declarations to
add missing claims.

Task 13 may emit an additive `measurement-v1` event with event kind
`comparison_attestation`, through the existing OTLP exporter/Collector path.
The event uses its original run/tenant/workflow and an existing declared root task.
Staging rejects (into `raw_rejections`) any attestation whose task is not an
expected root task of every staged declaration for that run, so it can neither
attach to an undeclared task nor inflate `unexpected_tasks`.

An attestation never changes the governed metrics of the run it attests: frozen
baselines are immutable evidence. `comparison_attestation` events are excluded
from event compatibility (`int_event_compat`), run evidence, task compatibility,
cost and completeness rollups (`mart_runs`), and from the run summary's identity
conflict count. Attestation integrity failures make only the comparison
ineligible ("missing arm provenance"); the run's CPST, costs and completeness are
byte-identical to the snapshot without the attestation (tested end to end for a
valid, a conflicting and an envelope-mismatched attestation). The attestation
envelope must therefore reproduce the attested run's declared reproducibility
fields exactly: `workflow_version`; `reproducibility.experiment_version`,
`cohort_version`, `config_version`, `code_revision` and `dataset_version`; and
`simulated` equal to whether the declaration's `measurement_mode` is
`fabricated`. Any mismatch leaves the comparison unavailable. More than one
declaration version for a run is its own ineligibility reason ("multiple
declaration versions").
Its payload contains:

```text
attestation_id = canonical SHA256(payload excluding attestation_id)
source_declaration_sha256 = canonical SHA256(original declaration)
provenance:
  source_manifest_sha256 = original replay manifest hash (or original null)
  config_version = original declaration's exact configuration identity
  dataset_version = original declaration's exact dataset identity
  config_artifact_sha256 = verified original configuration artifact bytes hash
  dataset_artifact_sha256 = verified original dataset/provenance artifact bytes hash
  comparison_artifact_sha256 = canonical SHA256(comparison object)
comparison:
  reference_version, business_config_id
  arm: config_version, model_version, prompt_version, question_version,
       calibration_id, evidence_version, retrieval_window, execution_mode, call_ids
```

Version IDs may be opaque names; separate artifact hashes are explicit. The trusted
workload-side producer must verify actual artifact bytes, the business-cost subset,
oracle policy, and any `not-applicable` claims before constructing the attestation.
Touchstone checks content and declared identity consistency; it cannot authenticate
external artifact contents it never reads. Hash binding is not an independent
review of those external claims. The platform never reads Reckoner files or accepts
a separate ingestion path.

The same published snapshot supplies the original declaration, companion event,
case membership, metrics and actual call IDs. Identical duplicate companions are
idempotent; conflicting companions, source/pin/hash mismatch, or an override of an
inline declaration claim leave comparison unavailable. Actual call IDs are read
from governed `int_calls`, overriding no operational identity. Inline provenance is
optional for new runs; an old run without a verified companion remains ineligible.

Baseline source inspected without rewriting it: runner manifest SHA256
`56778c8b73e9f087068e544cf6c42c09cd3c3f188b24698a8866ce8579611d8b`;
canonical declaration hashes tenant-a
`985c9002c964734eaffc9fe04b17d1cb862e638aaace2a5f64deeb22e0616d23`
and tenant-b
`4ea52990406a69b3b0c9f408e6f504976376588b935a9a196a45375c3f2a7036`.
The frozen baseline has 712/288 tenant members. This inspection does not attest
missing comparison claims or constitute a new paid baseline run.

## Measured bounded results

Final local report: `artifacts/phase3/task11/benchmark-report.json` and `.md`,
content ID `7b0325113e11fb42c7cdb04ba1eb9210e04834adfcdbd36ee69f0c0c267488ad`.
JSON SHA256 `276eaac24e0f4a81b477f84fc3de4b209556e15a8f5e68a944bcb4a16fb26569`.
`publication.json` records every underlying artifact's hash and byte count.
The original contaminated pass, replacement receipt, full exact memberships,
all top-five IDs/distances and raw timing/resource samples remain separate files.

All **7/7 neighbourhood results and 7/7 complete eligible sets agree**. There are
zero uncovered queries and five comparables for every query. Label agreement is
30/35 returned pairs: all 30 comparables for the six legitimate queries agree,
and none of the five for the single fraud query agrees. This is not evidence
of improved fraud decisions. Decision impact, model cost and CPST remain unavailable.

| Complete neighbourhood materialization | SQL p50 | SQL p99 | Cypher p50 | Cypher p99 |
| --- | ---: | ---: | ---: | ---: |
| Replacement first-after-restart, 7 observations | 414.044 ms | 2405.907 ms | 357.686 ms | 2199.147 ms |
| Original warm schedule, 35 observations | 389.746 ms | 548.748 ms | 225.942 ms | 454.576 ms |

The separate exact-vector adapter warm timings have p50 3900.794 ms and p99
4847.730 ms across 35 observations, including eligibility/coverage and query-feature
work. They are not compared to neighbourhood timings as equivalent APIs.

SQL's first-query inner plan emits 3,723 rows after reading 132,709 history rows
through `v1_history_time`, performing 132,709 merchant primary-key lookups and
filtering 128,986 rows. Sorting spills 638 blocks. Its diagnostic execution time
is 522.666 ms, measured after the timed schedule; this is not another benchmark
pass. Index definitions and both outer-function and inner SQL plans are retained.
No index or experiment parameter changed after observing the results.

Final accounting before the 85,159-byte report publication: 16,181,873,154 bytes
of Phase3 artifacts plus all preserved Phase3 volumes, and 29,832,548,352 bytes
host free. Both limits still pass after publication. This reconciliation includes
prior Task4–10 stores and Task11 fixture volumes, not only the benchmark database.
All Phase3 containers finish exited, OOMKilled=false, restart_count=0.
Warm-schedule sampled maxima are approximately 2.06 GiB Neo4j and 184.2 MiB
Postgres. These coarse observations miss peaks. Latest-start cgroup peaks are
2,556,063,744 and 672,550,912 bytes respectively, including the replacement pass
and later plan inspection; OOM counters are zero. These are not query-specific
peaks and do not replace earlier Task3 failure/import resource receipts.

## Tracked harness and offline reproduction

The original Task 11 measurement ran from preserved, untracked scratch scripts.
The same logic is now tracked under `reckoner.v1.benchmark` (`protocol.py`,
`resources.py`, `assembly.py`, `steps.py`) and exposed as
`reckoner v1 benchmark STEP`. Every step takes explicit paths; database URLs and
Neo4j credentials come only from environment variables whose NAMES are passed
(`--pg-dsn-env`, `--neo4j-user-env`, `--neo4j-password-env`). No environment
file is read, no credential is embedded, and no provider is called.

| Step | Role | Writes (never overwrites) |
| --- | --- | --- |
| `inventory` | Evaluator-side preparation. The only step that reads privileged `oracle.v1_resolutions`, to freeze each query's eligible candidate population. Read-only session. | `candidates.jsonl`, `protocol.json`, `sql-function-plan.json` |
| `prepare` | Candidate vectors from complete source history; preparation-role DSN; no oracle reads. One transaction with an explicit `COPY` column list: a failure stores no vectors, there is no resume, and a partial `vectors.jsonl` must be moved aside by the operator before a rerun. | `vectors.jsonl`, `preparation.json` |
| `verify` | Stored vector count/hash equals the frozen union; strict coverage for every query. Read-only session. | `stored-verification.json` |
| `measure` | Runtime-role SQL/Cypher schedule and exact top five; no labels; `--first-pass-state` records the operator's restart state. | `service-samples.jsonl`, `exact-memberships.json`, `retrieval-report.{json,md}` |
| `replacement` | One first pass, checked hash-for-hash against `measure`. | `replacement-service-samples.jsonl`, `replacement-first-pass.json` |
| `audit` | Read-only `du` of every volume mounted by matching containers, plus optional cgroup counters. | `resource-reconciliation-LABEL.json`, `cgroup-LABEL.json` |
| `report` | Offline evaluator assembly; joins query labels only after retrieval. | `--output` prefix `.json` and `.md`; a prefix that already ends in `.json` or `.md` is refused |

`measure` and `replacement` connect with `--runtime-role` (default
`reckoner_runner`) applied through libpq `options`, record the session's
`current_user`, `session_user` and `oracle` schema usage in the observation or
receipt, and refuse to run if the active role differs or can use schema `oracle`.
They also record the Neo4j user reported by the server (`neo4j_user`; the pinned
Community edition has a single database user, so this is identity, not a role).
Every store step first checks that all of its outputs are absent and runs the
resource guard before any timed or committed work (and again afterwards); for
`measure` and `replacement` that pre-flight guard sends no database query, so the
first timed pass still follows the restart. A failed service sample is never
silently lost: `measure` and `replacement` first write their finished one-shot
observation or receipt with `resource_samples_complete: false`, then exit with
an error. A complete sampler records `true`. `prepare` runs the guard, which may size volumes, at the start,
every `--guard-every` batches (default 10) and at the end. Candidate and stored ID
lists are ordered in Python, not by database collation, before any hash, and a
transaction ID appearing under two tenants is refused because the frozen union
hash is over transaction IDs. New protocols record the candidate-eligibility
policy (`simulated-seven-days-v1` resolutions, 90-day window, same tenant, strict
occurred/resolved cutoffs, positive amounts) that the inventory query uses; the
frozen Task 11 `protocol.json` predates that field and is unchanged.

The Cypher neighbourhood query text and evidence reader are now loaded once when
`CypherQueries` is constructed. The published first-pass and warm Cypher timings
were measured with the earlier code, which imported the module and read the
query file inside each timed call; they were not re-measured.

Resource guards default to a 15 GiB free-disk floor and a 20 GiB derived-data cap
(`--free-floor-bytes`, `--derived-cap-bytes`). Store sizes are either measured
from named volumes (`--store-volume NAME=VOLUME --du-image IMAGE`, read-only
mount) or explicit operator inputs (`--store-bytes NAME=BYTES`); each guard
record states which. Disclosure: the frozen Task 11 `protocol.json` resource
record carries a Neo4j store size of 3,789,230,080 bytes that the scratch script
took from the final Task 3 receipt rather than measuring. The final
reconciliation measured every mounted Phase 3 volume, including that store.

Interpretation text (limitations, relevance interpretation, plan observation,
resource accounting stage and replacement first-pass protocol) is an explicit
input, `docs/evidence/phase-3-benchmark-annotations.json`, copied verbatim into
report fields. It restates no numbers: every count lives in a computed field.
`sql-inner-plan.json`, `sql-indexes.json` and `cgroup-lifetime.json` were
diagnostic captures run by hand after measurement. They are pinned by hash in
the report but are not regenerated by tracked code; `audit --cgroup-container`
now covers the cgroup capture.

The expensive store steps were not re-run for this reproduction: no vector
re-preparation, database restart or new timing. The tracked `report` step is
run offline over the existing raw observation files into a fresh prefix:

```text
UV_PYTHON_INSTALL_DIR=/private/tmp/touchstone-uv-python \
UV_CACHE_DIR=/private/tmp/touchstone-uv-cache \
uv run --frozen --all-packages reckoner v1 benchmark report \
  --protocol artifacts/phase3/task11/protocol.json \
  --observation artifacts/phase3/task11/retrieval-report.json \
  --replacement artifacts/phase3/task11/replacement-first-pass.json \
  --resources artifacts/phase3/task11/resource-reconciliation-final.json \
  --cgroup artifacts/phase3/task11/cgroup-lifetime.json \
  --samples artifacts/phase3/task11/service-samples.jsonl \
  --samples artifacts/phase3/task11/replacement-service-samples.jsonl \
  --attach artifacts/phase3/task11/candidates.jsonl \
  --attach artifacts/phase3/task11/vectors.jsonl \
  --attach artifacts/phase3/task11/preparation.json \
  --attach artifacts/phase3/task11/stored-verification.json \
  --attach artifacts/phase3/task11/exact-memberships.json \
  --attach artifacts/phase3/task11/controlled-retrieval-report.json \
  --attach artifacts/phase3/task11/sql-function-plan.json \
  --attach artifacts/phase3/task11/sql-inner-plan.json \
  --attach artifacts/phase3/task11/sql-indexes.json \
  --attach artifacts/phase3/task11/regression.log \
  --oracle-labels artifacts/phase3/data/frozen-v1/oracle_validation.jsonl \
  --annotations docs/evidence/phase-3-benchmark-annotations.json \
  --output artifacts/phase3/task11-regenerated-r2/benchmark-report
```

First reproduction (fix round 1, annotations transcribed verbatim from the
published report, output prefix `artifacts/phase3/task11-regenerated/`): same
content ID `7b0325113e11fb42c7cdb04ba1eb9210e04834adfcdbd36ee69f0c0c267488ad`
and byte-identical JSON (SHA256
`276eaac24e0f4a81b477f84fc3de4b209556e15a8f5e68a944bcb4a16fb26569`). That proved
the tracked assembly reproduces every computed field.

Current reproduction (fix round 2, with the annotations above rewritten to restate
no unchecked numbers, output prefix `artifacts/phase3/task11-regenerated-r2/`):
content ID `4eb968125d729b4e2ea538da1f9d235b74041efecf17d9bbde5dc48eba892dcb`,
JSON SHA256 `76d5ed6e07cefbe25c0207628aa99b36b40f3a25e24df81f7319308b6ad4d3f7`,
Markdown SHA256 `8ee5ef353836e927b6466299627a5c983e1e0f75b1905ca69270cc1c69767eae`.
Its field-level difference from the published report is exactly the annotation
text and the resulting `report_id`; every measured or computed field is identical:

| Field | Published | Regenerated |
| --- | --- | --- |
| `limitations[0..7]` | compressed wording with restated counts (for example "June1 2018 seven-query", "full1441-user", "frozen20iterations") | the same eight statements without restated numbers |
| `plan_observation` | inner-plan row, lookup, spill and timing counts | points to the attached, hash-pinned `sql-inner-plan.json` |
| `vector_relevance_proxy.interpretation` | "one fraud query/all5retrievedlegitimate" | diagnostic-only note; the adjacent computed counts carry the numbers |
| `resources.accounting_stage` | same meaning, compressed wording | same meaning, full sentence |
| `cache_protocol.first_pass` | same meaning, compressed wording | same meaning, full sentence |
| `report_id` | `7b0325…7488ad` | `4eb968…892dcb` |

The regenerated Markdown also carries the explicit `measured-local-retrieval`
heading and label. The published files are unchanged.

## Real-store execution of the store steps (Task 12)

`workloads/reckoner/tests/test_v1_benchmark_stores.py` (markers `integration` and
`neo4j_integration`) runs `inventory`, a deliberately guard-failed `prepare`,
`prepare`, `verify`, a refused `measure`, `measure`, `replacement`, `audit` and
`report` once through the CLI against a disposable Postgres and Neo4j/GDS fixture
loaded from a tiny fabricated simulated source. It checks the active
`reckoner_runner` role and Neo4j user, refusal of an oracle-capable session,
pre-flight refusal of existing output, that a guard failure after the first `COPY`
batch leaves zero stored vectors, Python-sorted population hashes, stored-vector
reconciliation, sampler completeness and report assembly. It needs
`RECKONER_TEST_OWNER_DSN`, `RECKONER_TEST_NEO4J_URI`,
`RECKONER_TEST_SAMPLE_CONTAINERS` (comma-separated Docker names),
`RECKONER_TEST_AUDIT_PREFIX` and `RECKONER_TEST_DU_IMAGE`; it fails rather than skips
without them. This is fixture plumbing evidence, not a new measurement.

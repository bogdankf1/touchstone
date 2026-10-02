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
  calibration transfer across model-input arms. `comparison_eligibility` suppresses
  deltas for incomplete or incompatible membership, provenance, costs or refresh.
- `reckoner v1 compare --arms ARMS.json --expected IDS.json --output PREFIX`
  writes immutable, versioned JSON and Markdown without dispatching inference.
- Generic `run-declaration-v1.comparison` is optional. Old declarations remain
  valid and have unavailable comparison provenance. It declares reference/version,
  business-cost identity and model/input/evidence arm versions. `/v1/comparisons`
  reads both summaries from one immutable warehouse generation. Cross-tenant
  summaries sum governed numerators and denominators; excluded tenants block
  comparison. Synthetic workflows are never pooled with Reckoner.

The dashboard permits an explicit current-run selection, displays both arm
provenance records, and hides deltas when eligibility fails or generations change.
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

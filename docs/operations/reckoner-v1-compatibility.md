# Reckoner v1 records and compatibility

All workload data is simulated. V1 records use separate strict JSON schemas and
additive Postgres tables; baseline contracts, migrations 001–004, readers, and
measured artifacts retain their original meaning.

`reckoner.v1.contracts.validate_v1(kind, document)` accepts `run-config`, `evidence`,
`score`, `decision`, `case-note`, `review`, `resolution`, and `experiment`. Schema
filenames are `reckoner-{kind}-v1.schema.json`. Validation returns a detached
JSON document, rejects unknown fields and naive timestamps, and checks identities
and cross-field consistency. Probability and money values are decimal strings;
probabilities must be finite and within [0,1], and binary distributions sum exactly
to one. Configuration, evidence, decision, note, resolution, and experiment IDs
are SHA-256 hashes computed with the existing canonical `content_id` function,
excluding their own identity field. Call/action/task/run/case IDs are opaque data.

## Record vocabulary

Every record requires `schema_version` and `tenant_id`. Tests provide complete
fabricated examples in `workloads/reckoner/tests/v1_fixtures.py`.

| Kind | Fields beyond schema and tenant |
| --- | --- |
| run-config | `config_id`, `workflow_version`, `scorer`, `note_model`, `judge_model`, `threshold_config_id`, `feature_version`, `scaler_id`, `calibration_id`, `score_mode`, `graph_version`, `retrieval_version`, `resolution_policy_version`, `limits` |
| evidence | `evidence_id`, `transaction_id`, `query_time`, `source_snapshot_ids`, `cutoffs`, `coverage`, `features`, `risk_indicators`, `comparable_cases` |
| score | `run_id`, `task_id`, `call_id`, `raw_probability`, `distribution`, `confidence`, `requested_model`, `reported_model`, `question_version`, `input_sha256`, `usage`, `cost`, `attempt_status`, `adjusted_probability`, `calibration_id` |
| decision | `run_id`, `task_id`, `transaction_id`, `decision_id`, `outcome`, `config_id`, `evidence_id`, `call_id`, `raw_probability`, `effective_probability`, `effective_low_threshold`, `effective_high_threshold`, `scorer_status`, `degraded_reason`, `started_at`, `completed_at` |
| case-note | `note_id`, `case_id`, `decision_id`, `prompt_version`, `requested_model`, `reported_model`, `generation_status`, `evidence_id`, `call_id`, `started_at`, `completed_at`, and six content fields below |
| review | `case_id`, `action_id`, `decision_id`, `reviewer_id`, `reviewer_type`, `verdict`, `recommendation`, `idempotency_key`, `prior_case_version`, `reviewed_at`, `oracle_version`, `resolution_policy_version` |
| resolution | `resolution_id`, `transaction_id`, `verdict`, `oracle_version`, `resolved_at`, `resolution_policy_version`, `provenance` |
| experiment | `experiment_id`, `run_id`, `purpose`, `config_id`, `dataset_simulated`, `dataset_version`, `cohort_version`, `code_revision`, `created_at`, `tasks` |

Configuration model entries contain provider/model identity, question or prompt
version, and a complete immutable `price_table`. The scorer is binary
`jev-1.13.0` from `typesafe`; note/judge models initially use
`anthropic/claude-haiku-4-5-20251001`. Each nested price identity and model match is
validated. `limits` contains positive integer `timeout_seconds`,
`input_token_ceiling`, `max_output_tokens`, and `maximum_attempts`. A calibrated
score mode requires a calibration identity. These records grant no paid-call
permission and contain no provider key.

Evidence snapshot keys are `postgres` and nullable `graph`; cutoff keys are
`history_before`, `resolved_before`, and nullable `graph_before`. Cutoffs cannot
follow query time. Comparable resolutions must precede `resolved_before` strictly.
`features` is an explicit map of decimal measurements or missing (`null`) values.
Indicators contain consecutive `rank`, `indicator_id`, `description`, deterministic
`method`, and nonempty `evidence_refs`. Comparable entries contain tenant and
transaction identity, verdict, resolution timestamp/policy, similarity, and
references. Historical source construction and seven-day resolution derivation
are owned by the subsequent evidence task.

Scores use `distribution: {fraud, legitimate}`. A `responded` attempt requires
model identity, raw probability, confidence, and distribution; failed/uncertain
attempts cannot fabricate probabilities. Adjusted probabilities require a
calibration ID. `usage` records nullable input/output token counts. `cost` records
`status`, nullable `amount`, `currency: USD`, and `price_table_id`; settled cost
requires known usage and amount. Unknown usage remains explicit.

A `succeeded` decision requires call and probability values and agrees with the
effective thresholds. Equality at either threshold escalates. `failed`,
`uncertain`, or `unavailable` scoring requires a degraded reason and escalation;
raw/effective probabilities remain null. A nullable call ID does not fabricate a
successful provider response.

The six note content fields are `verdict_recommendation`, `confidence`,
`risk_indicators`, `entity_neighbourhood`, `comparable_cases`, and
`what_would_change_verdict`. They accompany required provenance. Confidence records
its `value` and `meaning` (`raw_choice_probability`, `calibrated_fraud_probability`,
or `unavailable`); unavailable confidence has no value. Summaries, comparables, and
actions carry evidence references. Empty indicators/comparables are explicit and
valid. Note generation statuses are `succeeded`, `failed`, or `degraded`. An
approve/decline note recommendation does not change the escalated workflow outcome.

Simulated reviews require oracle and resolution policy versions. Human reviews
carry null simulation references. Historical resolution provenance explicitly
states simulated label-oracle origin. Experiment manifests list unique task and
transaction IDs and can use `pilot`, `calibration`, `development`, `validation`,
`graph-comparison`, `final`, `judge`, or `fabricated` purposes independently of
baseline cohort constraints.

## Storage and roles

Apply migrations using the existing owner-only migration command. Migration 005
adds `v1_configs`, `v1_runs`, `v1_tasks`, `v1_evidence`, `v1_decisions`, `v1_cases`,
`v1_notes`, `v1_reviews`, and `v1_outbox` under `reckoner`. Reapplying identical SQL
is harmless; changed applied bytes fail the existing checksum gate. Do not edit
applied migrations. Backup/restore continues to cover the same database; new
tables must remain included in a complete Postgres backup.

`V1Repository(dsn)` is a context manager. Owner access registers validated
configuration snapshots and creates a tenant's run/tasks atomically. Transactions
are read from existing canonical records. The runner appends evidence and decisions.
Identical terminal retries return the persisted document; conflicting contents
raise `ValueError`. Decision persistence locks its task, verifies pinned config and
transaction/evidence references with composite foreign keys, creates one case for
an escalation (`case_id = decision_id`), and marks the task completed atomically.
Missing or foreign-tenant tasks raise `LookupError` from `task()`. Connection state
remains private to the repository; provider code receives detached documents only.

Documents and indexed identities cannot be rewritten, including through owner SQL;
status/version transitions remain separate. Runner privileges permit only needed
append operations and task/outbox status updates. API access is initially read-only
through `api_v1_*` views; it has no base-table or oracle access. The existing
runner/evaluator/API separation remains. Note evidence is pinned to its case's
decision. Review/settings operational writes and provider accounting/checkpoints
belong to later tasks, which extend focused storage modules and additive migrations.

## Python dependency evidence

Python remains `>=3.12,<3.13`. The existing lock already resolves HTTPX 0.28.1;
Reckoner now declares `httpx==0.28.1` directly for its imminent scorer adapter,
without changing transitive versions. [HTTPX release metadata](https://pypi.org/pypi/httpx/0.28.1/json)
requires Python >=3.8 and HTTPcore 1.x. Both agree with this workspace lock (HTTPcore 1.0.9). The lock also already
contains Pydantic 2.13.5 and OTel 1.37.0, which fall within the prospective
constraints below.

The following exact prospective pins were checked against primary package metadata
on 2026-09-30; they are deferred to their owning tasks and are not installed by this
storage stage. Metadata compatibility is not a completed joint resolver/runtime
validation.

| Owner | Prospective pin | Metadata constraints relevant to this workspace |
| --- | --- | --- |
| Calibration | `numpy==2.5.3`, `scipy==1.18.1` | Both require Python >=3.12; SciPy requires NumPy >=2.0,<2.8 |
| Orchestration | `langgraph==1.2.12` | Python >=3.10; LangChain Core >=1.4.7,<2, checkpoint >=4.1,<5, Pydantic >=2.7.4 |
| Note evaluation | `deepeval==4.2.7` | Python >=3.9,<4; Pydantic >=2.11.7,<3; OTel API/SDK >=1.24,<2 |
| Faithfulness evaluation | `ragas==0.4.3` | Python >=3.9; NumPy >=1.21,<3; Pydantic >=2; OpenAI >=1; LangChain dependencies require joint resolution |

Sources: [NumPy](https://pypi.org/pypi/numpy/2.5.3/json),
[SciPy](https://pypi.org/pypi/scipy/1.18.1/json),
[LangGraph](https://pypi.org/pypi/langgraph/1.2.12/json),
[DeepEval](https://pypi.org/pypi/deepeval/4.2.7/json),
[Ragas](https://pypi.org/pypi/ragas/0.4.3/json).
Task 1 uses standard-library `Decimal`; its controller explicitly deferred numeric
pins until calibration to avoid adding unused runtime dependencies. Later tasks
must resolve their exact pins against `uv.lock`, exercise their integrations, and
record any necessary compatible substitutions before use.

Hatch's existing schema inclusion covers all eight new schemas. The installed-wheel
check validated each schema through `validate_v1` from a separate installation
outside the checkout and verified migration 005 is packaged. `neo4j_integration`
is registered now and excluded from offline CI; GDS integration infrastructure
belongs to its later task.

## Temporal graph and vector evidence (Task 3)

Migration 007 requires Postgres 17 with pgvector 0.8.6. Compose, Kubernetes and CI
now pin `pgvector/pgvector:0.8.6-pg17-bookworm` at index digest
`sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f`.
Its inspected arm64 manifest is
`sha256:de5bb95ded567f98e342a29f188f8053b2e8d344cb9ccd52cbdfe15f720cfde7`.
No service credentials, preserved volumes, Postgres major or existing resource
settings changed. This is a prerequisite for Task 12, not a deployment receipt.

The dedicated Task 3 recipe pins Neo4j Community 2026.09.0 at index digest
`sha256:91fb0bf237c41b7b3dcbe84703aa0b82e0d7d067b16e1c8ab21f03fc679edf4e`
(arm64 manifest `sha256:93a04808534ace5c812eeb77cc08df1f01607b970fd270263f7ccaf4940666be`).
GDS 2026.09.0 JAR SHA-256 is
`74e7026ed7bad144c67473a0e4d47276907b781ee5e283ecab11245498ad2a8e`;
`infra/prepare-reckoner-v1-test.sh` checks these bytes. Its bundled license is
GPLv3; community runtime reports `gds.isLicensed() = false`. Actual projection,
Louvain and PageRank calls passed on this pair with `neo4j==6.0.3`.
[Official installation](https://neo4j.com/docs/graph-data-science/current/installation/installation-docker/),
[version compatibility](https://neo4j.com/docs/graph-data-science/current/installation/supported-neo4j-versions/)
and [GDS license](https://github.com/neo4j/graph-data-science/blob/master/LICENSE.txt)
provide the upstream references. The pinned legacy `gds.graph.project.cypher`
procedure works and emits a deprecation notice; upgrade work must migrate it.

`fit_scaler(development)` accepts weighted `{purpose: "development", weight,
transaction, history}` observations from 2017 only. Numeric coordinates use
weighted inverse-CDF median/IQR and missing coordinates independently; unavailable
history retains the known amount coordinate. The frozen development scaler uses
all 2,000 members (200 fraud / 1,800 legitimate); the pilot is a subset, not an
exclusion. Runtime `feature_vector` requires available history and verifies the
scaler content hash. Candidate history must precede the candidate timestamp.

Task documents passed to adapters are `{transaction: <canonical transaction>,
query_time: <same occurrence instant>}`; `query_time` may be omitted. The private
runtime adapter input is `{...<validated persisted run-config>, scaler:
<detached scaler JSON>}`. Persist only the strict run-config containing `scaler_id`;
load and hash-check the detached artifact before calling the adapter. Task 3's
artifact is `artifacts/phase3/task3/resource-scaler.json`, scaler identity
`a5fd8dbaa782b6c5cf66e947f6ba13faca07dea5be2fefc231da939949e10dbb`, file SHA-256
`8bf9170296d8f34c6d8d747847e9027fd3fade78dd5853b4a46dfff59dcf863a`.

Evidence adds optional strict `neighbourhood` and `graph_projection` fields.
The display is bounded to 100 nodes/200 edges, with complete transaction references,
totals and truthful truncation. Projection metadata records actual cutoff, identity,
age, algorithm/version/parameters, coverage counts and PageRank convergence.
Unavailable history/projection/vector population is distinct from verified empty
results. Later snapshots are never used for an earlier query. GDS communities and
centrality do not themselves create risk factors. Louvain uses default resolution;
its selected procedure has no random seed input (recorded `seed_supported: false`).
[Algorithm reference](https://neo4j.com/docs/graph-data-science/current/algorithms/louvain/).
PageRank retains damping .85, maximum 20 iterations, tolerance 1e-7 and concurrency
one; nonconvergence remains in stored and returned evidence.
[PageRank reference](https://neo4j.com/docs/graph-data-science/current/algorithms/page-rank/).

Exact pgvector L2 search ranks five cases, ties by transaction ID; reported
similarity is `1/(1+L2)`. SQL and Cypher candidate eligibility is same tenant,
positive supported purchase, occurrence strictly earlier than query, resolution
strictly earlier than query and at least query minus 90 days. All historical
records/resolutions remain retained, including nonpositive events; excluded
candidate counts appear as `excluded_unsupported_comparables`. Runner roles receive
cutoff functions/coverage view, not direct histories, vectors or oracle access.

The bounded simulated-source receipt proves one June 1 projection and seven frozen
queries, not full operational import or complete real vector retrieval. Real vectors
were deliberately omitted and their coverage is unavailable; fabricated integration
exercises actual pgvector top-five search. Task 11 must measure equivalent SQL/Cypher
work with controlled caches and investigate relational query plans; exploratory
adapter times do not establish a graph speed advantage. Full operational import,
all-query coverage and real candidate-vector preparation remain later gates.

Task 3 review adds migration `008_v1_evidence_scope.sql`; applied 007 remains
unchanged. `v1_shared_merchant_scope` exposes declared tenant scope through a
runner-only cutoff-adapter helper. Neo4j stores owner-supplied `MerchantIdentity`
declarations independently of observed history, including declared tenants with
zero imported events. Both adapters require declared shared-merchant scope and
coverage through the query with its 97-day lookback. Missing declarations or
incomplete foreign coverage yield explicit partial evidence; neighbourhood totals
are omitted and merchant-exposure ratios/indicators are suppressed. Valid own-card
features and same-tenant cases remain usable. Projection identity validation hashes
stored receipt contents excluding `projection_id` and query-derived
`snapshot_age_seconds`; convergence and other body fields cannot drift under an
existing identity. PostgreSQL-only CI excludes the Neo4j integration marker.

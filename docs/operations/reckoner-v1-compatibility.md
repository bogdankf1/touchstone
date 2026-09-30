# Reckoner v1 records and compatibility

All workload data is simulated. V1 records use separate strict JSON schemas and
additive Postgres tables; baseline contracts, migrations 001–004, readers, and
measured artifacts retain their original meaning.

`reckoner.v1.contracts.validate_v1(kind, document)` accepts `run-config`, `evidence`,
`score`, `decision`, `case-note`, `review`, `resolution`, and `experiment`. Schema
filenames are `reckoner-{kind}-v1.schema.json`. Validation returns a detached
JSON document, rejects unknown fields and naive timestamps, and checks identities
and cross-field consistency. Probability and money values are decimal strings;
probabilities must be finite and within [0,1], and binary distributions sum to one within 1e-6. Raw probabilities are preserved;
the validator does not normalize them. Configuration, evidence, decision, note, resolution, and experiment IDs
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

## Jev attempts and provider accounting (Task 4)

Migration 009 adds protected operational `v1_protocols`, `v1_protocol_closures`,
`v1_provider_calls`, `v1_provider_responses`, `v1_settlements`, provider-wide
`v1_provider_state` and `v1_legacy_provenance`. Call/protocol/response/settlement
joins use tenant-qualified foreign keys. Immutable records permit append only;
API roles cannot read provider bodies or accounting tables. Provider-wide state
uses the explicit `_provider-wide` operational tenant marker.

`JevClient(api_key, transport=None).evaluate(request)` makes one HTTP attempt,
with 5-second connect and 30-second response timeouts and disabled transport
retries. It requires explicitly supplied `JEV_API_KEY`; it does not read an SDK
default variable. JSON fractional numbers retain exact decimal text, and
nonfinite JSON is rejected while retaining the protected body. Bearer headers
never enter persisted request documents or public errors. No account call or
paid inference was executed in Task 4; all provider tests use fabricated transport.

`build_request(transaction, evidence)` projects canonical fields and validates a
strict evidence record before serialization. Provider inputs include a simulated-data
marker, evidence coverage, cutoffs, deterministic risk indicators, comparables
and graph convergence limitations. They exclude oracle/source fields and personal
payment attributes. Neighbourhood transaction references now use the same stable
`content_id([tenant_id, "transaction", transaction_id])` as transaction display
nodes, preserving opaque identities across tenants without delimiter ambiguity.

`score_task(repo, client, task, evidence, protocol)` returns a strict score record.
A provider skip instead returns `{scorer_status: "unavailable", degraded_reason:
"..."}` with no invented call or probability. `BudgetExceeded` propagates and
leaves unexecuted tasks incomplete. Task 5 applies calibration to raw scores;
Task 6 must persist response/accounting before advancing its workflow checkpoint.
Responses are keyed by immutable call identity: completed responses are reused;
missing responses after reservation become uncertain and cannot auto-retry.
Transient/connect responses can resume only within the same pinned request,
protocol and maximum three attempts. Unknown usage retains the attempt maximum.
Rate deadlines and Retry-After deferral survive restart. Five consecutive
transient/connect failures open a 60-second circuit; one persisted active call
provides sequential dispatch and the single half-open probe. Retry backoff is
1/2 seconds with deterministic request-seeded jitter; hints over 60 seconds defer.

`ProviderBudget(connection).reserve(call, Decimal(maximum), protocol)` creates a
provider-wide envelope once and allocates individual attempt maxima within it.
The envelope and allocations are one accounting hierarchy. Open envelopes retain
their cap; `close(protocol_id)` releases only unused capacity while preserving
settled costs and unresolved maxima. `settle(call_id, usage, cost)` appends an
immutable uncertain or settled event, permits uncertain-to-known reconciliation,
and rejects conflicting repeated known settlements. Cost/token overages block
further dispatch for that provider. Jev and Anthropic totals remain independent.

The original legacy ledger must exist in the measurement database. Owner-only
`verify_legacy()` checks 1,040 actual settled ledger entries totaling USD .493151
and records tenant-tagged content-hash provenance under advisory lock 732019102.
It does not create entries or subtract a hardcoded balance. New Anthropic
reservations and the scoring CLI require matching provenance; an empty database
fails the gate. All existing legacy liability is read exactly once under the same
lock. During Phase 3 measurement, stop legacy dispatch operationally; the v0
Anthropic request implementation and budget enforcement were left unchanged.

`reckoner v1 score --manifest PATH --protocol PATH --env-file PATH` requires an
already registered immutable experiment, prepared evidence, and a runner-only
file containing `RECKONER_RUNNER_DSN` and `JEV_API_KEY` (or `--env-file -` for explicit
environment loading). The protocol is an immutable content-hashed document:
`tenant_id`, `run_id`, `provider`, `purpose`, `model`, `input_token_ceiling`,
`max_output_tokens`, `maximum_attempts`, decimal `usd_cap`, `approved: true`,
`tasks: [{task_id, transaction_id, request_sha256}]`, and `protocol_id`. Cases
must match the manifest exactly; each hash selects one prepared evidence/request.
All cases, configuration/model/question/pricing and bounds are checked before
any dispatch. This artifact represents separately obtained concrete paid-run
approval; approving the implementation does not authorize producing or executing
one. CLI returns task statuses without protected bodies or secrets.

Input bounds use the complete request's UTF-8 byte count as a conservative local
text-token bound, avoiding an invented provider tokenizer. Output usage is checked
after response; the documented Jev endpoint exposes no output-limit parameter.
Actual provider usage, account access, billing credits, original-ledger restoration
and the measured paid-run gates remain unexecuted owner dependencies.

## Verified numeric calibration dependencies (Task 5)

The workspace now declares exact `numpy==2.5.3`, `scipy==1.18.1`, and
`matplotlib==3.11.2` runtime pins
in Reckoner and records both in `uv.lock`. `uv lock` resolved 190 packages, adding
only these two numeric packages. Adding the standard scientific plotting library
then resolved 197 packages, adding Matplotlib and six required dependencies
(contourpy 1.4.0, cycler 0.12.1, fonttools 4.66.1, kiwisolver 1.5.1, Pillow 12.3.0,
pyparsing 3.3.3); `uv sync --frozen --all-packages` installed their
macOS arm64 wheels with CPython 3.12.13 and uv 0.11.32. Existing HTTPX 0.28.1,
Pydantic 2.13.5 and OTel 1.37.0 remain resolved. Actual SciPy constrained optimization,
NumPy weighted calculations, seeded cluster bootstrap, Matplotlib headless SVG
publication and CLI execution passed on
this joint environment. These pins replace the earlier prospective calibration
metadata evidence; other tasks' prospective pins remain deferred.

Calibration artifacts are standalone, evaluator-produced JSON using the existing
`content_id` convention, not a new kind accepted by `validate_v1`. The strict run
configuration still records only the selected `calibration_id` and `score_mode`.
The separate explicit context validator and artifact/export formats are documented
in [Phase 3 experiments](../data/phase-3-experiments.md#offline-calibration-diagnostics-task-5).

## Durable workflow runtime (Task 6)

Pinned runtime: `langgraph==1.2.12`, `langgraph-checkpoint==4.1.0`, CPython3.12.
Migration010 adds tenant/run policy bindings, task evidence/protocol bindings,
checkpoint payloads and pending node writes. The synchronous tenant-scoped
`PostgresCheckpointer(dsn, tenant_id)` implements the supported
`BaseCheckpointSaver` `get_tuple`, `list`, `put` and `put_writes` interfaces using
LangGraph's typed serializer. A separate database connection handles checkpoint
writes; graph execution uses synchronous durability. This implementation supports
this synchronous graph; async execution, unscoped checkpoint listing, deletion,
forking and administrative replay are not exposed.

Use `V1Repository(runner_dsn, scorer_client=client)` and
`build_graph(checkpointer)`. The client is an injected runtime dependency, never a
checkpoint or JSON field. `run_task(repo, graph, task, config)` accepts exactly:

- `evidence_id`: a previously persisted strict evidence document for that task's
  transaction and query time. Persist an explicit unavailable bundle when mandatory
  evidence cannot be obtained; never invent a successful evidence record.
- `evidence_mode`: `relational` or `gds-augmented`. This is independently supplied
  experiment policy, never copied from a calibration artifact. Graph sharing is
  evidence provenance rather than another treatment. Relational policy rejects
  graph references; GDS policy with no projection degrades to escalation.
- `data_kind`: explicitly `fabricated` or `simulated-cctd`, supplied by the caller's
  established experiment inputs. These software tests use fabricated records only.
- `protocol`: the strict Task4 approved protocol, or null when mandatory evidence
  is unavailable and no scoring dispatch can happen. This interface does not grant
  paid-run approval. Available evidence requires a protocol before binding the task.
- `calibration`: the selected Task5 artifact, or null in raw mode. Runtime checks
  its identity against the pinned run configuration and calls
  `validate_calibration_context` with independently pinned scorer, feature, scaler,
  graph, retrieval, evidence-mode and data-kind context before numerical apply.

The first task atomically pins evidence mode, data kind and calibration at **run**
scope; all later tasks must match. Task scope additionally pins evidence, protocol,
configuration and initial timestamp. Changed inputs conflict, including after a
completed task. New settings versions cannot rewrite an existing run. Full source
histories, oracle labels, clients and credentials never enter graph state; it holds
identities, bounded status categories, probabilities and effective thresholds.
PageRank nonconvergence remains explicit alongside the immutable evidence reference.

`resume_task(repo, graph, tenant_id, run_id, task_id)` uses persisted bindings.
Both entrypoints hold a task-scoped PostgreSQL advisory lock for the full lifecycle,
including HTTP, so concurrent workers cannot mistake an active call for a crashed
one. Each worker owns its repository connection. Opaque IDs are encoded as a JSON
identity hash for the LangGraph thread; tenant/run/task columns remain separate in
all checkpoint queries and foreign keys. Checkpoint writes reject substituted
identity/configuration/evidence/calibration/protocol values.

Task4 response and billing records remain authoritative if a checkpoint fails.
Restart at the score node consumes the saved response without another HTTP request;
ambiguous delivery stays uncertain and budget exhaustion propagates with no decision.
The decision and escalation case commit atomically before terminal graph success.
A missing note does not prevent manual review. Task7 owns note generation; this
Task6 graph ends after decision persistence and does not mark a run's billing complete.
The generated graph is `docs/architecture/reckoner-v1-workflow.mmd`, produced with
`build_graph(InMemorySaver()).get_graph().draw_mermaid()`.

### Task7 note/evaluation implementation

Installed and exercised DeepEval4.2.7 `BaseMetric`/`LLMTestCase` and Ragas0.4.3 modern
`collections.Faithfulness`/`InstructorBaseRagasLLM` with injected responses. Joint
resolution needs `langchain-community==0.4.1`;0.4.2 removes `chat_models.vertexai`,
which Ragas0.4.3 imports. No provider/account endpoint or paid compatibility claim.
Each actual framework request passes through Task4 accounting; no parser/library
retries are active. See [note evaluation](note-evaluation.md) for exact stage bounds,
late-note persistence, runtime attachment and Task13 staged-protocol handoff.

Case-note confidence adds `jev_distribution_concentration`. Generation copies Jev's
separate confidence field, never raw/calibrated probability or estimated accuracy.
Older schema enum spellings remain readable; the new factual validator rejects them
for newly generated notes. No baseline contract or artifact is regenerated.

Task7 review hardening binds durable evaluation to the actual persisted note work,
published note, decision/evidence/scorer/configuration, and exact saved generation
request. Missing/failed notes and caller substitutions cannot pass. Generation and
Ragas share `build_note_context`, with distinct raw/effective routing probabilities,
frozen calibration identity and unchanged Jev concentration. This changes prepared
request bytes/hashes; old exact-hash approvals do not approve the revised request.

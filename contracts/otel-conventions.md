# Reckoner OTLP conventions

Reckoner v0 records provider evidence as one serialized OTLP
`ExportTraceServiceRequest` per completed or failed provider attempt. The project pins the
OpenTelemetry GenAI semantic convention document version to **v1.37.0**. This is the project's
chosen version and is not a claim that v1.37.0 is the latest convention.
The vocabulary comes from the upstream
[v1.37.0 GenAI span conventions](https://github.com/open-telemetry/semantic-conventions/blob/v1.37.0/docs/gen-ai/gen-ai-spans.md).

The runtime pins `opentelemetry-sdk` and `opentelemetry-proto` to 1.37.0 and
`opentelemetry-semantic-conventions` to 0.58b0. Provider spans use
`gen_ai.operation.name=chat`, `gen_ai.provider.name=anthropic`, requested/reported model names,
and available input/output token counts. Billable cost appears only on the provider span.

Project attributes use the `touchstone.*` namespace for workflow, tenant, run, task, config,
cohort, event, and call identities. Traces never contain prompts, model response bodies,
credentials, source records, or oracle labels. A measured task uses a genuine 16-byte trace ID
and 8-byte span ID persisted before dispatch. Explicit fake-mode test runs remain distinguishable
through durable run mode and the `touchstone.provider_call_mode=fake` attribute, and never satisfy
a paid-pilot gate.

Generic `measurement-v1` JSON documents are validated against
`contracts/schemas/measurement-v1.schema.json`, encoded as named
`touchstone.measurement.<event_kind>` span events, and stored in the
`touchstone.measurement.json` attribute as canonical compact JSON. Outcome currency is `USD`
only when a cost is known; unknown costs and currency remain null. Future outcome and evaluation
spans link to the task trace rather than duplicating provider cost.

Touchstone v1 run expectations use `contracts/schemas/run-declaration-v1.schema.json`. A
declaration is a named `touchstone.run.declaration` span event whose
`touchstone.run.json` attribute contains canonical compact JSON. It declares tenant,
workflow, run, distinct expected task IDs and count, experiment versions, simulated-source
status, measurement mode, replay provenance, and required evaluation checks/cases. It carries
expectations, never calculated results. A replay declaration identifies its frozen source
manifest by SHA-256; historical provider spans are not rewritten. Repeated identical
declarations have the same canonical SHA-256 identity. A changed declaration for the same
tenant/workflow/run identity is a conflict that ingestion must surface.
Optional `metric_expectations` entries declare each generic metric ID, definition version,
unit, and expected task IDs. Without a matching expectation, observed contribution sums
remain visible but their rate has unknown coverage and is unavailable.

At extraction, the platform validates each measurement JSON document and separately compares
its tenant, workflow, workflow version, run, task, simulation flag, trace ID, and span ID with
the enclosing span. The validator's `received_at` is ingestion time, independent of the
envelope's `occurred_at`; neither field is inferred from a file path.

Local export writes the exact protobuf bytes from the durable outbox using deterministic names
and SHA-256 checksums. `manifest.json` preserves ordered filenames, checksums, event IDs, run IDs,
tenant IDs, and task IDs. Replay sends those unchanged bytes to `/v1/traces` using OTLP/HTTP
protobuf and treats any `partial_success.rejected_spans` value as incomplete delivery.

For this simulated workload, every task, provider, and evaluator span carries
`touchstone.dataset_simulated=true`. This describes the transaction data and is independent of
provider execution. Canonical transaction `provenance.simulated` remains true. On all three span
kinds, `touchstone.provider_call_mode` is the persisted `fake` or `measured` mode;
`touchstone.simulated` and each measurement envelope's `simulated` are true only for `fake`
(fabricated provider measurements). A measured call on simulated transactions sets these two
measurement indicators to false without implying real customer transactions.

Negative, boolean, missing, or otherwise invalid provider token counters remain unchanged in the
operational response and usage evidence. Generic telemetry represents such counters as null
(and omits their GenAI counter attributes); their cost remains unavailable.

Touchstone v1 accepts these OTLP/HTTP protobuf requests at the Collector and writes traces to
ClickHouse using the pinned `clickhouseexporter` v0.136.0 trace schema. Its `Events.Name` and
`Events.Attributes` nested arrays preserve each measurement or declaration JSON attribute.
`Timestamp` remains the original span start time. The custom raw table adds server-assigned
`ReceivedAt` (`DateTime64(6, 'UTC')`) and `ReceiptId` (UUID) defaults for ingestion progress.
It also materializes `tenant_id` from the span attribute. An invalid raw span with a missing
or mismatched tenant has no trusted tenant assignment; extraction emits a rejection rather than
creating an attributed measurement or declaration.
Extractor refreshes scan **all** raw receipts through one fixed UTC cutoff in bounded pages,
ordered by `(ReceivedAt, ReceiptId)`. The UUID disambiguates timestamp ties; the microsecond
precision matches Python's exact `datetime` cursor precision. Late-arriving old events therefore
enter a later refresh. The staging refresh must record its cutoff plus extracted, rejected and
declaration counts in its operator receipt; current events without a valid declaration never imply complete
task coverage.

The Collector uses a file-backed exporter queue on a named volume and does not expire raw trace
rows during Phase 2 acceptance. An OTLP 200 response acknowledges queue acceptance; extraction
must still confirm ClickHouse receipts. The queue is bounded and a full queue can reject new
requests, which replay reports as pending. Before any finite raw-data retention is enabled,
choose a policy longer than the maximum refresh outage and replay window, verify warehouse
materialization and a recoverable backup, then prove late arrivals remain visible across the
boundary. A disposable Compose `queue-init` container sets ownership of the queue volume to
the pinned Collector image's UID/GID 10001; the Collector itself remains non-root.

The operational response artifact allowlist is `provider_request_id`, `requested_model`,
`reported_model`, `finish_reason`, `content`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
and `cache_creation_tokens`; absent fields are null and invalid received values are retained.
`response_sha256` hashes this exact persisted JSON document as UTF-8, sorted object keys, compact
`,`/`:` separators, unescaped Unicode, and no non-finite numbers. Artifact, checksum, settlement,
and outbox are committed atomically, including invalid-but-received responses. No received
response means no artifact or checksum. JSON/Markdown reports link tenant/task/call identities
to the checksum without including response text. Existing artifacts/outbox bytes are not rewritten.

## Additive root-work lifecycle and scoped accounting

Existing Phase 2 declarations/events retain their original interpretation. New producers can
opt in before execution with `run-declaration-v1.lifecycle_version="root-work-v1"`.
`expected_task_ids` remains the exact immutable root population. `evaluation_suites` names the
permitted suite versions/checks before any result is available. Optional `provider_prices`
contains `{provider, model, price_table_version}` bindings; distinct models keep their distinct
price snapshots. Conflicting snapshots for the same provider/model make cost unavailable.

The additional `measurement-v1` event kinds are:

- `work_declaration`: root `task_id`; payload `{child_task_ids, evaluation_suites}`. Both arrays
  are explicit, including empty arrays. Each suite has the existing suite/version/check shape
  and exact case IDs. There is one immutable declaration per root. New case populations come
  from this declaration, never from successful results. Missing/conflicting declarations,
  undeclared suites/checks, or missing declared child executions prevent closure. A root
  must itself have an unambiguous terminal execution (completed or failed); declarations
  and closures alone never establish execution or complete cost.
- `work_closure`: root `task_id`; payload `{cost_scope: "online"|"offline", call_ids: string[],
  work_status: "completed"|"failed", billing_status: "complete"|"uncertain"}`. Each scope has
  its own immutable closure and exact actual call identities. Unknown usage, missing/extra
  calls, conflicting identities or uncertain billing cannot become a complete zero subtotal.
  Failed terminal work with fully settled calls can have complete cost while quality fails.

`provider_usage.payload.cost_scope` is optional for compatibility: absent means `online`.
Online `model_cost` and CPST exclude offline calls. `offline_model_cost`, `provider_spend`,
`online_cost_complete`, and `offline_cost_complete` are additional generic run/API fields.
The same fields appear on task node rows; node `model_cost` is online, while the dashboard
node table labels total `provider_spend` explicitly. Node `model_cost` and
`offline_model_cost` are the node's observed scope costs: run-level scope incompleteness
withholds run totals and provider spend but not observed node cost, and the completeness flags
stay on the node row. A node scope amount is null when any of its calls in that scope has an
unknown amount or the node mixes currencies. Provider spend requires both scopes
complete and compatible currencies. An uncertain offline
judge leaves online model cost available independently; required missing quality checks still
leave overall metrics incomplete. Calls retain their original task/run/provider identities,
including judges. There are no duplicated costs in shadow runs or node-name classifications.

A review's `recommendation` may be null; then `agreement` is null, not false. Optional review
`started_at` retains actual task intake and pairs with `reviewed_at` for lead time. Note work
uses an explicit child execution; its duration starts at intake while the root duration ends
at the persisted decision. Child completion never changes root decision timestamps.

Reckoner's producer maps purpose to scope at the workload boundary: final scoring and
`online-note` are online; `pilot`, `calibration`, `development`, `validation`, `graph-comparison`
and `judge` are offline. `fabricated` scoring fixtures use online scope but retain fabricated
measurement provenance. All transaction sources remain simulated. No raw cases, prompts,
responses, graph text, hidden labels, credentials, or evidence bodies cross OTLP.

### Durable producer handoff

`V1Repository.create_run` atomically stores the run declaration with root membership.
`persist_decision` atomically stores the root execution and required work declaration with
case creation. `measurement_events(run, task, outcomes)` maps persisted workload bundles;
`run` contains `manifest` and `config`, and `task` contains `decision`, normalized `calls`,
optional `note_result`, `reviews`, and `evaluations`. Domain outcomes and metric contributions
are calculated in Reckoner with the frozen threshold/oracle inputs.

`enqueue_events(repo, events)` validates and inserts exact deterministic OTLP protobuf bytes;
matching identities are idempotent, changed content conflicts. Usage events are deferred
until an authoritative cost settlement is available: uncertain or unanswered calls stay in
operational storage and prevent closure, while previously delivered bytes remain immutable.
Interim exported call counts count settled observations, not all dispatched calls, and cannot
claim final coverage without closure. `collect_run(repo, tenant_id,
run_id)` maps late persisted calls/notes/reviews/evaluations. `evaluate_run` requires the
existing evaluator role and emits only derived outcomes/contributions. `close_scope(repo,
*, tenant_id, run_id, task_id, scope)` checks terminal work and settled exact calls; admission
and closure lock the same task row, and new calls to a closed scope are rejected by SQL.
`export_pending(repo, exporter, *, limit)` marks delivery only on a successful OTLP HTTP
protobuf response with zero rejected spans. Collector interruption or partial rejection
retains pending exact bytes for retry; ambiguous acknowledgement can duplicate receipts,
which the warehouse deduplicates.

The Task 8 `v1_outbox` JSON source documents, IDs, UTF-8 PostgreSQL JSON bytes, and timestamps
remain immutable. Migration 013 adds a separate `v1_otlp_delivery` table. Mapping preserves
the original review event ID and recommendation snapshot; it never rewrites the source.

Commands use DSN-only files, with `--env-file -` reading the named DSN from the environment:

```text
reckoner v1 telemetry-collect --tenant-id ID --run-id ID --env-file RUNNER_FILE
reckoner v1 telemetry-collect-scoring --tenant-id ID --run-id ID --env-file RUNNER_FILE
reckoner v1 telemetry-evaluate --tenant-id ID --run-id ID --env-file EVALUATOR_FILE
reckoner v1 telemetry-close --tenant-id ID --run-id ID --task-id ID --scope online|offline --env-file RUNNER_FILE
reckoner v1 telemetry-export --endpoint http://collector:4318 --limit 500 --env-file RUNNER_FILE
```

Runner files contain only `RECKONER_RUNNER_DSN`; evaluator files contain only
`RECKONER_EVALUATOR_DSN`. Export sends pending delivery rows across the runner's permitted
local dataset. No command reads a provider key or dispatches inference.

Offline Task 4 score-only runs are created with
`create_run(manifest, config_id, telemetry_mode="scoring-only")` (the default is `workflow`).
This mode is immutable and independent of purpose; either collector rejects a mismatched mode.
They use `collect_scoring_run` with an
explicit offline scoring purpose (`pilot`, `calibration`, `development`, `validation`, `graph-comparison`). Their run
has no decision metric or note-suite expectations. Empty per-root work is declared at run
creation. Actual admission/response timestamps produce a scoring execution; there is no
fabricated routing decision, outcome, or note. All source task protocols must be closed using
`ProviderBudget.close(protocol_id)` before terminal scoring work and scope closure; unresolved
billing remains incomplete. A score-only execution stays started until billing is settled;
then persisted attempt outcomes determine completed (a valid scoring response) versus failed
(no valid response). A billed failed score contributes spend but never successful completion
or successful-latency population. Task 13 must finish all intended calls before closing telemetry
scopes, keep source IDs for reused results, and close online and offline separately. A
score-only run can expose complete provider spend while decision outcomes/CPST remain absent.

`reconcile_report(local_report, warehouse_report, expected)` compares every independently
specified expected path, including tenant/run/task IDs and a caller-pinned warehouse
generation. Decimal strings compare exactly; null/missing is never zero. The independent
simulated receipt is `workloads/reckoner/expectations/reckoner-v1-local.json`. It demonstrates
software plumbing, not measured provider or quality acceptance.

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

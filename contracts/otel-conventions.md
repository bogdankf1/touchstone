# Reckoner OTLP conventions

Reckoner v0 records provider evidence as one serialized OTLP
`ExportTraceServiceRequest` per completed or failed provider attempt. The project pins the
OpenTelemetry GenAI semantic convention document version to **v1.37.0**. This is the project's
chosen version and is not a claim that v1.37.0 is the latest convention.

The runtime pins `opentelemetry-sdk` and `opentelemetry-proto` to 1.37.0 and
`opentelemetry-semantic-conventions` to 0.58b0. Provider spans use
`gen_ai.operation.name=chat`, `gen_ai.provider.name=anthropic`, requested/reported model names,
and available input/output token counts. Billable cost appears only on the provider span.

Project attributes use the `touchstone.*` namespace for workflow, tenant, run, task, config,
cohort, event, and call identities. Traces never contain prompts, model response bodies,
credentials, source records, or oracle labels. A measured task uses a genuine 16-byte trace ID
and 8-byte span ID persisted before dispatch. Explicit fake-mode test runs remain distinguishable
through their durable run mode and never satisfy a paid-pilot gate.

Generic `measurement-v1` JSON documents are validated against
`contracts/schemas/measurement-v1.schema.json`, encoded as named
`touchstone.measurement.<event_kind>` span events, and stored in the
`touchstone.measurement.json` attribute as canonical compact JSON. Outcome currency is `USD`
only when a cost is known; unknown costs and currency remain null. Future outcome and evaluation
spans link to the task trace rather than duplicating provider cost.

Local export writes the exact protobuf bytes from the durable outbox using deterministic names
and SHA-256 checksums. `manifest.json` preserves ordered filenames, checksums, event IDs, run IDs,
tenant IDs, and task IDs. Replay sends those unchanged bytes to `/v1/traces` using OTLP/HTTP
protobuf and treats any `partial_success.rejected_spans` value as incomplete delivery.

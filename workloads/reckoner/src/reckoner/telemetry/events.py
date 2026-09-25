"""Create SDK spans and atomically store their exact OTLP protobuf bytes."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import ValidationError
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import IdGenerator, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult, SpanProcessor
from opentelemetry.trace import Status, StatusCode

from reckoner.contracts import validate_document

ROOT = Path(__file__).resolve().parents[5]
MEASUREMENT_SCHEMA = ROOT / "contracts" / "schemas" / "measurement-v1.schema.json"


class _FixedIdGenerator(IdGenerator):
    def __init__(self, trace_id: str, span_id: str):
        self._trace_id = int(trace_id, 16)
        self._span_id = int(span_id, 16)

    def generate_span_id(self) -> int:
        return self._span_id

    def generate_trace_id(self) -> int:
        return self._trace_id


class _CaptureProcessor(SpanProcessor):
    def __init__(self):
        self.spans: list[ReadableSpan] = []

    def on_start(self, span, parent_context=None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        self.spans.append(span)

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class DurableOTLPExporter(SpanExporter):
    """Serialize ended SDK spans and synchronously pass exact bytes to a durable sink."""

    def __init__(self, sink: Callable[[bytes], None]):
        self._sink = sink

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        request = encode_spans(spans)
        self._sink(request.SerializeToString(deterministic=True))
        return SpanExportResult.SUCCESS


def encode_measurement_event(document: dict[str, Any]) -> tuple[str, str]:
    """Validate the generic envelope before returning its OTel event name and JSON body."""
    try:
        validate_document(document, MEASUREMENT_SCHEMA)
    except (ValidationError, OSError, TypeError, ValueError) as error:
        raise ValueError("invalid measurement document") from error
    body = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return f"touchstone.measurement.{document['event_kind']}", body


def add_measurement_event(span, document: dict[str, Any]) -> None:
    """Attach one validated generic envelope as a named SDK span event."""
    name, body = encode_measurement_event(document)
    span.add_event(name, {"touchstone.measurement.json": body})


def _nanoseconds(timestamp: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(timestamp).timestamp() * 1_000_000_000)


def _attributes(
    *,
    attempt: dict,
    result: dict | None,
    status: str,
    error_category: str | None,
    actual_cost: Decimal | None,
    usage: dict | None,
    price_table_version: str,
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": attempt["request_document"]["model"],
        "touchstone.workflow_id": "reckoner",
        "touchstone.workflow_version": "baseline-v0",
        "touchstone.tenant_id": attempt["tenant_id"],
        "touchstone.run_id": attempt["run_id"],
        "touchstone.task_id": attempt["task_id"],
        "touchstone.config_id": attempt["config_id"],
        "touchstone.cohort_id": attempt["cohort_id"],
        "touchstone.event_id": attempt["event_id"],
        "touchstone.call_id": attempt["call_id"],
        "touchstone.simulated": True,
        "touchstone.provider.mode": ("fake" if attempt["execution_mode"] == "test" else "measured"),
        "touchstone.status": status,
        "touchstone.price_table_version": price_table_version,
    }
    if result is not None and isinstance(result.get("reported_model"), str):
        attributes["gen_ai.response.model"] = result["reported_model"]
    if usage is not None:
        for source, target in (
            ("input_tokens", "gen_ai.usage.input_tokens"),
            ("output_tokens", "gen_ai.usage.output_tokens"),
        ):
            value = usage.get(source)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                attributes[target] = value
    if actual_cost is not None:
        attributes["touchstone.provider_cost"] = format(actual_cost, "f")
        attributes["touchstone.provider_cost_currency"] = "USD"
    if error_category is not None:
        attributes["error.type"] = error_category
    return attributes


def store_provider_span(
    cursor,
    *,
    attempt: dict,
    result: dict | None,
    status: str,
    error_category: str | None,
    timing: dict,
    actual_cost: Decimal | None,
    usage: dict | None,
    price_table_version: str,
) -> None:
    """End, encode, and insert one provider span using the caller's transaction."""
    capture = _CaptureProcessor()
    provider = TracerProvider(
        resource=Resource.create({"service.name": "reckoner", "service.version": "baseline-v0"}),
        id_generator=_FixedIdGenerator(attempt["trace_id"], attempt["span_id"]),
    )
    provider.add_span_processor(capture)
    tracer = provider.get_tracer("reckoner.baseline", "1.0")
    span = tracer.start_span(
        "reckoner.provider.chat",
        start_time=_nanoseconds(timing["started_at"]),
        attributes=_attributes(
            attempt=attempt,
            result=result,
            status=status,
            error_category=error_category,
            actual_cost=actual_cost,
            usage=usage,
            price_table_version=price_table_version,
        ),
    )
    span.set_status(Status(StatusCode.OK if status == "completed" else StatusCode.ERROR))
    span.end(end_time=_nanoseconds(timing["ended_at"]))
    if len(capture.spans) != 1:
        raise RuntimeError("provider span did not end synchronously")

    def insert(payload: bytes) -> None:
        cursor.execute(
            """
            INSERT INTO reckoner.runner_telemetry_outbox
              (tenant_id, event_id, run_id, task_id, payload, producer)
            VALUES (%s, %s, %s, %s, %s, 'runner')
            """,
            (
                attempt["tenant_id"],
                attempt["event_id"],
                attempt["run_id"],
                attempt["task_id"],
                payload,
            ),
        )

    exported = DurableOTLPExporter(insert).export(capture.spans)
    if exported is not SpanExportResult.SUCCESS:
        raise RuntimeError("OTLP serialization failed")

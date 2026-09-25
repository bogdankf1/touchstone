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
from opentelemetry.trace import Status, StatusCode, set_span_in_context

from reckoner.contracts import validate_document

ROOT = Path(__file__).resolve().parents[5]
MEASUREMENT_SCHEMA = ROOT / "contracts" / "schemas" / "measurement-v1.schema.json"


class _FixedIdGenerator(IdGenerator):
    def __init__(self, trace_id: str, span_ids: Sequence[str]):
        self._trace_id = int(trace_id, 16)
        self._span_ids = iter(int(span_id, 16) for span_id in span_ids)

    def generate_span_id(self) -> int:
        return next(self._span_ids)

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
        "touchstone.provider_call_mode": attempt["provider_call_mode"],
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


def _reproducibility(attempt: dict) -> dict[str, str | None]:
    return {
        "experiment_version": "baseline-v0",
        "cohort_version": attempt["cohort_id"],
        "config_version": attempt["config_id"],
        "code_revision": attempt["preflight_code_revision"],
        "prompt_version": attempt["config"]["prompt_version"],
        "model_version": attempt["request_document"]["model"],
        "scorer_version": None,
        "dataset_version": attempt["bundle_id"],
        "graph_version": None,
    }


def _measurement(
    *, attempt: dict, event_kind: str, span_id: str, occurred_at: str, payload: dict
) -> dict:
    return {
        "schema_version": "measurement-v1",
        "event_id": f"{attempt['event_id']}:{event_kind}",
        "tenant_id": attempt["tenant_id"],
        "workflow_id": "reckoner",
        "workflow_version": "baseline-v0",
        "run_id": attempt["run_id"],
        "task_id": attempt["task_id"],
        "trace_id": attempt["trace_id"],
        "span_id": span_id,
        "node_name": "task" if event_kind == "execution" else "provider.chat",
        "event_kind": event_kind,
        "occurred_at": occurred_at,
        "simulated": attempt["provider_call_mode"] == "fake",
        "reproducibility": _reproducibility(attempt),
        "payload": payload,
    }


def _cached_tokens(usage: dict | None) -> int | None:
    if usage is None:
        return None
    values = [usage.get("cache_read_tokens"), usage.get("cache_creation_tokens")]
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in values
    ):
        return None
    return sum(values)


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
    """End, encode, and insert linked task/provider spans in the caller transaction."""
    capture = _CaptureProcessor()
    provider = TracerProvider(
        resource=Resource.create({"service.name": "reckoner", "service.version": "baseline-v0"}),
        id_generator=_FixedIdGenerator(
            attempt["trace_id"], [attempt["task_span_id"], attempt["span_id"]]
        ),
    )
    provider.add_span_processor(capture)
    tracer = provider.get_tracer("reckoner.baseline", "1.0")
    task_span = tracer.start_span(
        "reckoner.task",
        start_time=_nanoseconds(timing["started_at"]),
        attributes={
            "touchstone.workflow_id": "reckoner",
            "touchstone.workflow_version": "baseline-v0",
            "touchstone.tenant_id": attempt["tenant_id"],
            "touchstone.run_id": attempt["run_id"],
            "touchstone.task_id": attempt["task_id"],
            "touchstone.config_id": attempt["config_id"],
            "touchstone.cohort_id": attempt["cohort_id"],
            "touchstone.event_id": attempt["event_id"],
            "touchstone.simulated": attempt["provider_call_mode"] == "fake",
            "touchstone.status": status,
        },
    )
    span = tracer.start_span(
        "reckoner.provider.chat",
        context=set_span_in_context(task_span),
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
    add_measurement_event(
        task_span,
        _measurement(
            attempt=attempt,
            event_kind="execution",
            span_id=attempt["task_span_id"],
            occurred_at=timing["ended_at"],
            payload={
                "started_at": timing["started_at"],
                "ended_at": timing["ended_at"],
                "duration_ms": timing["duration_ms"],
                "status": "completed" if status == "completed" else "failed",
                "attempt_number": 1,
                "parent_task_id": None,
                "parent_span_id": None,
                "provider": None,
                "model": None,
            },
        ),
    )
    input_tokens = usage.get("input_tokens") if usage is not None else None
    output_tokens = usage.get("output_tokens") if usage is not None else None
    add_measurement_event(
        span,
        _measurement(
            attempt=attempt,
            event_kind="provider_usage",
            span_id=attempt["span_id"],
            occurred_at=timing["ended_at"],
            payload={
                "provider": "anthropic",
                "model": (
                    result.get("reported_model")
                    if result is not None and isinstance(result.get("reported_model"), str)
                    else attempt["request_document"]["model"]
                ),
                "call_id": attempt["call_id"],
                "input_tokens": input_tokens if isinstance(input_tokens, int) else None,
                "output_tokens": output_tokens if isinstance(output_tokens, int) else None,
                "cached_tokens": _cached_tokens(usage),
                "cost_status": "actual" if actual_cost is not None else "unavailable",
                "cost_amount": format(actual_cost, "f") if actual_cost is not None else None,
                "currency": "USD" if actual_cost is not None else None,
                "price_table_version": (price_table_version if actual_cost is not None else None),
            },
        ),
    )
    span.set_status(Status(StatusCode.OK if status == "completed" else StatusCode.ERROR))
    span.end(end_time=_nanoseconds(timing["ended_at"]))
    task_span.set_status(Status(StatusCode.OK if status == "completed" else StatusCode.ERROR))
    task_span.end(end_time=_nanoseconds(timing["ended_at"]))
    if len(capture.spans) != 2:
        raise RuntimeError("task/provider spans did not end synchronously")

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

"""Allowlisted deterministic OTLP protobuf and acknowledgement-aware HTTP delivery."""

import json
import urllib.request
from datetime import datetime

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from reckoner.contracts import content_id, validate_document
from reckoner.resources import SCHEMAS


def encode(document):
    declaration = document["schema_version"] == "run-declaration-v1"
    validate_document(document, SCHEMAS / (document["schema_version"] + ".schema.json"))
    request = ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    attr = resource.resource.attributes.add(key="service.name")
    attr.value.string_value = "reckoner"
    scope = resource.scope_spans.add()
    scope.scope.name = "reckoner.v1"
    span = scope.spans.add()
    span.trace_id = bytes.fromhex(
        content_id(["declaration-trace", document["event_id"]])[:32]
        if declaration
        else document["trace_id"]
    )
    span.span_id = bytes.fromhex(
        content_id(["declaration-span", document["event_id"]])[:16]
        if declaration
        else document["span_id"]
    )
    span.name = "touchstone.run.declaration" if declaration else document["node_name"]
    at = document["declared_at"] if declaration else document["occurred_at"]
    timestamp = int(datetime.fromisoformat(at).timestamp() * 1_000_000_000)
    span.start_time_unix_nano = timestamp
    span.end_time_unix_nano = timestamp
    for key in (
        "tenant_id",
        "workflow_id",
        "workflow_version",
        "run_id",
        *(() if declaration else ("task_id",)),
    ):
        span.attributes.add(key="touchstone." + key).value.string_value = document[key]
    simulated = document.get("simulated", document.get("measurement_mode") == "fabricated")
    span.attributes.add(key="touchstone.dataset_simulated").value.bool_value = True
    span.attributes.add(key="touchstone.provider_call_mode").value.string_value = (
        "fake" if simulated else "measured"
    )
    if not declaration:
        span.attributes.add(key="touchstone.simulated").value.bool_value = simulated
        if document["event_kind"] == "provider_usage":
            payload = document["payload"]
            for key, value in {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": payload["provider"],
                "gen_ai.request.model": payload["model"],
                "touchstone.call_id": payload["call_id"],
            }.items():
                span.attributes.add(key=key).value.string_value = value
            for field in ("input_tokens", "output_tokens"):
                if payload[field] is not None:
                    attr = span.attributes.add(key="gen_ai.usage." + field)
                    attr.value.int_value = payload[field]
    event = span.events.add(
        name="touchstone.run.declaration"
        if declaration
        else "touchstone.measurement." + document["event_kind"],
        time_unix_nano=timestamp,
    )
    event.attributes.add(
        key="touchstone.run.json" if declaration else "touchstone.measurement.json"
    ).value.string_value = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return request.SerializeToString(deterministic=True)


class OTLPExporter:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint.rstrip("/")
        if not self.endpoint.endswith("/v1/traces"):
            self.endpoint += "/v1/traces"

    def export(self, payload: bytes) -> dict:
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/x-protobuf"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 200:
                    return {"accepted": False, "rejected_spans": 0}
                result = ExportTraceServiceResponse.FromString(response.read())
        except Exception:
            # Transport diagnostics can contain credentials; only safe status leaves this boundary.
            return {"accepted": False, "rejected_spans": 0}
        rejected = result.partial_success.rejected_spans
        return {"accepted": rejected == 0, "rejected_spans": rejected}

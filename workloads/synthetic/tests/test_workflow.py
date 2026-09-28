"""The fabricated second workload must produce reproducible, contract-valid OTLP."""

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from touchstone_synthetic.events import build_requests

SCHEMAS = Path(__file__).resolve().parents[3] / "contracts" / "schemas"


def _decode(payloads):
    declarations = []
    measurements = []
    for payload in payloads:
        request = ExportTraceServiceRequest.FromString(payload)
        for resource in request.resource_spans:
            for scope in resource.scope_spans:
                for span in scope.spans:
                    attributes = {item.key: item.value for item in span.attributes}
                    for event in span.events:
                        document = json.loads(event.attributes[0].value.string_value)
                        if event.name == "touchstone.run.declaration":
                            declarations.append(document)
                        else:
                            assert event.name == f"touchstone.measurement.{document['event_kind']}"
                            measurements.append(document)
                            assert attributes["touchstone.simulated"].bool_value
                            assert attributes["touchstone.dataset_simulated"].bool_value
                            assert span.trace_id.hex() == document["trace_id"]
                            assert span.span_id.hex() == document["span_id"]
    return declarations, measurements


def test_complete_fixture_has_exact_populations_costs_and_valid_envelopes():
    declarations, events = _decode(build_requests("synthetic-proof"))
    assert len(declarations) == 2
    assert {d["tenant_id"]: d["expected_task_ids"] for d in declarations} == {
        "synthetic-tenant-a": ["case-a1", "case-a2"],
        "synthetic-tenant-b": ["case-b1", "case-b2"],
    }
    assert all(
        d["measurement_mode"] == "fabricated" and d["dataset_simulated"] for d in declarations
    )
    assert all(e["simulated"] for e in events)
    assert Counter(
        e["payload"]["status"]
        for e in events
        if e["event_kind"] == "execution" and e["payload"]["parent_task_id"] is None
    ) == {"completed": 3, "failed": 1}
    assert sum(e["payload"]["correct"] is True for e in events if e["event_kind"] == "outcome") == 2
    assert sum(
        Decimal(e["payload"]["cost_amount"]) for e in events if e["event_kind"] == "provider_usage"
    ) == Decimal("0.10")
    assert sum(
        Decimal(e["payload"]["review_cost"]) for e in events if e["event_kind"] == "outcome"
    ) == Decimal("4")
    assert sum(
        Decimal(e["payload"]["error_cost"]) for e in events if e["event_kind"] == "outcome"
    ) == Decimal("2")
    assert (Decimal("0.10") + Decimal("4") + Decimal("2")) / 2 == Decimal("3.05")
    assert {e["node_name"] for e in events if e["event_kind"] == "provider_usage"} == {
        "classify",
        "case_note",
    }
    assert {e["payload"]["status"] for e in events if e["event_kind"] == "evaluation"} == {
        "pass",
        "fail",
        "error",
    }
    assert all(
        e["payload"]["judge_model_version"]
        and e["payload"]["judge_prompt_version"]
        and e["payload"]["supporting_references"]
        for e in events
        if e["event_kind"] == "evaluation" and e["payload"]["suite_id"] == "synthetic-case-note-v1"
    )
    for kind, documents in (("run-declaration-v1", declarations), ("measurement-v1", events)):
        validator = Draft202012Validator(
            json.loads((SCHEMAS / f"{kind}.schema.json").read_text()),
            format_checker=FormatChecker(),
        )
        for document in documents:
            validator.validate(document)


def test_repeated_emission_preserves_logical_identities():
    first = _decode(build_requests("repeatable"))
    second = _decode(build_requests("repeatable"))
    for left, right in zip(first, second, strict=True):
        assert [(d["event_id"], d.get("trace_id"), d.get("span_id")) for d in left] == [
            (d["event_id"], d.get("trace_id"), d.get("span_id")) for d in right
        ]


def test_missing_required_check_is_declared_but_absent():
    declarations, events = _decode(build_requests("synthetic-proof", incomplete=True))
    assert len(declarations) == 2
    assert all(d["run_id"] == "synthetic-proof-incomplete" for d in declarations)
    assert any(
        "reference_faithfulness" in suite["required_checks"]
        for d in declarations
        for suite in d["evaluation_suites"]
    )
    assert not any(
        e["event_kind"] == "evaluation" and e["payload"]["metric_id"] == "reference_faithfulness"
        for e in events
    )

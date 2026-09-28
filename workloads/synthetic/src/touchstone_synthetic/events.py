"""Emit a fixed fabricated workflow through the shared OTLP contract."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

WORKFLOW = "synthetic-case-triage"
START = datetime(2026, 9, 26, 12, tzinfo=UTC)
TASKS = (
    ("synthetic-tenant-a", "case-a1", "completed", True, "classify", "0.01", "0", "0", "pass"),
    ("synthetic-tenant-a", "case-a2", "completed", False, "case_note", "0.04", "4", "0", "fail"),
    ("synthetic-tenant-b", "case-b1", "completed", True, "classify", "0.02", "0", "0", "pass"),
    ("synthetic-tenant-b", "case-b2", "failed", False, "case_note", "0.03", "0", "2", "error"),
)
SCHEMAS = Path(__file__).parent / "_resources"
if not SCHEMAS.exists():
    SCHEMAS = Path(__file__).resolve().parents[4] / "contracts" / "schemas"


def _id(value: str, length: int) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:length]


def _timestamp(offset_ms: int = 0) -> str:
    return (START + timedelta(milliseconds=offset_ms)).isoformat().replace("+00:00", "Z")


def _validated(document: dict, version: str) -> dict:
    schema = json.loads((SCHEMAS / f"{version}.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    return document


def _attribute(collection, key: str, value: str | bool) -> None:
    attribute = collection.add()
    attribute.key = key
    if isinstance(value, bool):
        attribute.value.bool_value = value
    else:
        attribute.value.string_value = value


def _request(document: dict, *, declaration: bool) -> bytes:
    request = ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    _attribute(resource.resource.attributes, "service.name", "touchstone-synthetic")
    span = resource.scope_spans.add().spans.add()
    span.trace_id = bytes.fromhex(
        document.get(
            "trace_id", _id(f"{document['run_id']}:{document['tenant_id']}:declaration", 32)
        )
    )
    span.span_id = bytes.fromhex(document.get("span_id", _id(document["event_id"], 16)))
    span.name = "synthetic.declaration" if declaration else f"synthetic.{document['node_name']}"
    instant = int(START.timestamp() * 1_000_000_000)
    span.start_time_unix_nano = instant
    span.end_time_unix_nano = instant + 1_000_000
    for field in ("tenant_id", "workflow_id", "workflow_version", "run_id"):
        _attribute(span.attributes, f"touchstone.{field}", document[field])
    _attribute(span.attributes, "touchstone.dataset_simulated", True)
    if not declaration:
        _attribute(span.attributes, "touchstone.task_id", document["task_id"])
        _attribute(span.attributes, "touchstone.simulated", True)
    event = span.events.add()
    event.name = (
        "touchstone.run.declaration"
        if declaration
        else f"touchstone.measurement.{document['event_kind']}"
    )
    event.time_unix_nano = instant
    _attribute(
        event.attributes,
        "touchstone.run.json" if declaration else "touchstone.measurement.json",
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
    )
    return request.SerializeToString()


def _declaration(tenant: str, task_ids: list[str], run_id: str, *, incomplete: bool) -> dict:
    checks = ["note_quality", "reference_faithfulness"] if incomplete else ["note_quality"]
    return _validated(
        {
            "schema_version": "run-declaration-v1",
            "event_id": f"{run_id}:{tenant}:declaration",
            "tenant_id": tenant,
            "workflow_id": WORKFLOW,
            "workflow_version": "synthetic-v1",
            "run_id": run_id,
            "declared_at": _timestamp(),
            "expected_task_count": len(task_ids),
            "expected_task_ids": task_ids,
            "experiment_version": "synthetic-v1",
            "cohort_version": "four-fabricated-cases-v1",
            "config_version": "fixture-v1",
            "code_revision": "synthetic-fixture-v1",
            "dataset_version": "fabricated-cases-v1",
            "measurement_mode": "fabricated",
            "dataset_simulated": True,
            "replay": {"is_replay": False, "source_manifest_sha256": None},
            "evaluation_suites": [
                {
                    "suite_id": "synthetic-case-note-v1",
                    "suite_version": "v1",
                    "expected_case_ids": task_ids,
                    "required_checks": checks,
                }
            ],
            "metric_expectations": [
                {
                    "metric_id": "correctness",
                    "definition_version": "synthetic-v1",
                    "unit": "case",
                    "expected_task_ids": task_ids,
                }
            ],
        },
        "run-declaration-v1",
    )


def _measurement(run_id: str, tenant: str, task: str, node: str, kind: str, payload: dict) -> dict:
    event_id = f"{run_id}:{tenant}:{task}:{kind}:{node}"
    return _validated(
        {
            "schema_version": "measurement-v1",
            "event_id": event_id,
            "tenant_id": tenant,
            "workflow_id": WORKFLOW,
            "workflow_version": "synthetic-v1",
            "run_id": run_id,
            "task_id": task,
            "trace_id": _id(f"{run_id}:{tenant}:{task}", 32),
            "span_id": _id(event_id, 16),
            "node_name": node,
            "event_kind": kind,
            "occurred_at": _timestamp(100),
            "simulated": True,
            "reproducibility": {
                "experiment_version": "synthetic-v1",
                "cohort_version": "four-fabricated-cases-v1",
                "config_version": "fixture-v1",
                "code_revision": "synthetic-fixture-v1",
                "prompt_version": "synthetic-note-prompt-v1" if node == "case_note" else None,
                "model_version": "fabricated-model-v1"
                if node in {"classify", "case_note"}
                else None,
                "scorer_version": "synthetic-judge-v1" if kind == "evaluation" else None,
                "dataset_version": "fabricated-cases-v1",
                "graph_version": None,
            },
            "payload": payload,
        },
        "measurement-v1",
    )


def build_requests(run_id: str, *, incomplete: bool = False) -> list[bytes]:
    """Build deterministic protobuf requests; every number is a fabricated fixture value."""
    if not run_id or not run_id.strip():
        raise ValueError("run_id is required")
    actual_run = f"{run_id}-incomplete" if incomplete else run_id
    requests = []
    for tenant in ("synthetic-tenant-a", "synthetic-tenant-b"):
        task_ids = [row[1] for row in TASKS if row[0] == tenant]
        requests.append(
            _request(
                _declaration(tenant, task_ids, actual_run, incomplete=incomplete), declaration=True
            )
        )
    for tenant, task, status, correct, node, model_cost, review_cost, error_cost, check in TASKS:
        documents = [
            _measurement(
                actual_run,
                tenant,
                task,
                "workflow",
                "execution",
                {
                    "started_at": _timestamp(),
                    "ended_at": _timestamp(100),
                    "duration_ms": 100,
                    "status": status,
                    "attempt_number": 1,
                    "parent_task_id": None,
                    "parent_span_id": None,
                    "provider": None,
                    "model": None,
                },
            ),
            _measurement(
                actual_run,
                tenant,
                task,
                node,
                "provider_usage",
                {
                    "provider": "synthetic-generator",
                    "model": "fabricated-model-v1",
                    "call_id": f"{actual_run}:{task}:call",
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cached_tokens": 0,
                    "cost_status": "estimated",
                    "cost_amount": model_cost,
                    "currency": "USD",
                    "price_table_version": "fabricated-price-v1",
                },
            ),
            _measurement(
                actual_run,
                tenant,
                task,
                "evaluate",
                "outcome",
                {
                    "status": "observed",
                    "correct": correct,
                    "review_cost": review_cost,
                    "error_cost": error_cost,
                    "currency": "USD",
                    "outcome_version": "synthetic-oracle-v1",
                    "evaluator_version": "synthetic-judge-v1",
                },
            ),
            _measurement(
                actual_run,
                tenant,
                task,
                "evaluate",
                "evaluation",
                {
                    "suite_id": "synthetic-case-note-v1",
                    "case_id": task,
                    "metric_id": "note_quality",
                    "score": {"pass": 1, "fail": 0, "error": None}[check],
                    "threshold": 1,
                    "status": check,
                    "judge_model_version": "fabricated-judge-v1",
                    "judge_prompt_version": "synthetic-judge-prompt-v1",
                    "supporting_references": [f"fabricated-reference:{task}"],
                },
            ),
            _measurement(
                actual_run,
                tenant,
                task,
                "evaluate",
                "metric_contribution",
                {
                    "metric_id": "correctness",
                    "numerator": 1 if correct else 0,
                    "denominator": 1,
                    "unit": "case",
                    "definition_version": "synthetic-v1",
                    "cost_component_id": None,
                },
            ),
        ]
        requests.extend(_request(document, declaration=False) for document in documents)
    return requests

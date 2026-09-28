"""Phase 1 declaration sidecars use identities and suites, never result values."""

import hashlib
import json
from pathlib import Path

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from touchstone_platform.declarations import build_declarations


def _source(path, name, *, evaluation=False):
    path.mkdir()
    request = ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add()
    span.trace_id = b"\x01" * 16
    span.span_id = b"\x02" * 8
    for field, value in (
        ("tenant_id", "tenant-a"),
        ("workflow_id", "reckoner"),
        ("workflow_version", "baseline-v0"),
        ("run_id", "run-1"),
    ):
        attribute = span.attributes.add()
        attribute.key = f"touchstone.{field}"
        attribute.value.string_value = value
    event = span.events.add()
    event.name = (
        "touchstone.measurement.evaluation" if evaluation else "touchstone.measurement.execution"
    )
    attribute = event.attributes.add()
    attribute.key = "touchstone.measurement.json"
    attribute.value.string_value = json.dumps(
        {
            "tenant_id": "tenant-a",
            "workflow_id": "reckoner",
            "workflow_version": "baseline-v0",
            "run_id": "run-1",
            "task_id": "task-1",
            "event_kind": "evaluation" if evaluation else "execution",
            "occurred_at": "2026-09-26T10:00:00Z",
            "reproducibility": {
                "experiment_version": "exp-1",
                "cohort_version": "cohort-1",
                "config_version": "config-1",
                "code_revision": "revision-1",
                "dataset_version": "dataset-1",
            },
            "payload": {
                "case_id": "task-1",
                "suite_id": "required-quality-v1",
                "metric_id": "decision_correctness",
                "status": "fail",
                "score": 0,
            }
            if evaluation
            else {"status": "completed"},
        }
    )
    payload = request.SerializeToString()
    (path / "one.pb").write_bytes(payload)
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "request_count": 1,
                "requests": [
                    {
                        "filename": "one.pb",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "tenant_id": "tenant-a",
                        "run_id": "run-1",
                        "task_id": "task-1",
                    }
                ],
            }
        )
    )


def _expectations(path: Path, runner: Path, evaluator: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "phase1-replay-expectations-v1",
                "workflow_id": "reckoner",
                "workflow_version": "baseline-v0",
                "evaluation_suites": [
                    {
                        "suite_id": "required-quality-v1",
                        "suite_version": "v1",
                        "required_checks": ["decision_correctness"],
                        "case_membership": "all_runner_tasks",
                    }
                ],
                "metric_expectations": [
                    {
                        "metric_id": "correctness",
                        "definition_version": "reckoner-metrics-v1",
                        "unit": "decision",
                        "task_membership": "all_runner_tasks",
                    }
                ],
                "sources": [
                    {
                        "run_id": "run-1",
                        "runner_manifest_sha256": hashlib.sha256(
                            (runner / "manifest.json").read_bytes()
                        ).hexdigest(),
                        "evaluator_manifest_sha256": hashlib.sha256(
                            (evaluator / "manifest.json").read_bytes()
                        ).hexdigest(),
                    }
                ],
            }
        )
    )
    return path


def test_sidecar_uses_membership_and_required_checks_without_outcome_values(tmp_path):
    runner, evaluator = tmp_path / "runner", tmp_path / "evaluator"
    _source(runner, "runner")
    _source(evaluator, "evaluator", evaluation=True)
    expectations = _expectations(tmp_path / "expectations.json", runner, evaluator)
    declarations = build_declarations(runner, evaluator, expectations)
    assert len(declarations) == 1
    document = declarations[0]
    assert document["expected_task_ids"] == ["task-1"]
    assert document["evaluation_suites"] == [
        {
            "suite_id": "required-quality-v1",
            "suite_version": "v1",
            "expected_case_ids": ["task-1"],
            "required_checks": ["decision_correctness"],
        }
    ]
    assert document["measurement_mode"] == "measured"
    assert "status" not in json.dumps(document)
    assert "score" not in json.dumps(document)


def test_omitted_evaluation_and_contribution_remain_expected(tmp_path):
    runner, evaluator = tmp_path / "runner", tmp_path / "evaluator"
    _source(runner, "runner")
    _source(evaluator, "evaluator")  # no evaluation or contribution event
    expectations = _expectations(tmp_path / "expectations.json", runner, evaluator)

    document = build_declarations(runner, evaluator, expectations)[0]

    assert document["evaluation_suites"][0]["required_checks"] == ["decision_correctness"]
    assert document["evaluation_suites"][0]["expected_case_ids"] == ["task-1"]
    assert document["metric_expectations"][0]["metric_id"] == "correctness"
    assert document["metric_expectations"][0]["expected_task_ids"] == ["task-1"]


def test_wrong_independent_manifest_pin_is_rejected(tmp_path):
    runner, evaluator = tmp_path / "runner", tmp_path / "evaluator"
    _source(runner, "runner")
    _source(evaluator, "evaluator", evaluation=True)
    expectations = _expectations(tmp_path / "expectations.json", runner, evaluator)
    document = json.loads(expectations.read_text())
    document["sources"][0]["runner_manifest_sha256"] = "0" * 64
    expectations.write_text(json.dumps(document))

    with pytest.raises(ValueError, match="expectation source pin"):
        build_declarations(runner, evaluator, expectations)

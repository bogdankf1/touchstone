"""Allowlisted mapping of persisted simulated workload records."""

import importlib
import json
from copy import deepcopy

import pytest
from v1_fixtures import config_fixture, decision_fixture, evidence_fixture, experiment_fixture


def telemetry():
    try:
        return importlib.import_module("reckoner.v1.telemetry.events")
    except ModuleNotFoundError:
        pytest.fail("v1 measurement mapping is missing")


def bundles():
    config = config_fixture()
    manifest = experiment_fixture(config)
    decision = decision_fixture(config, evidence_fixture(), degraded=True)
    run = {"manifest": manifest, "config": config}
    task = {
        "decision": decision,
        "calls": [],
        "note_result": None,
        "evaluations": [],
        "reviews": [],
    }
    return run, task


def test_pending_note_declares_population_without_claiming_zero_cost():
    run, task = bundles()
    task["raw_case"] = "NEVER-EXPORT"
    events = telemetry().measurement_events(run, task, [])
    assert {e["event_kind"] for e in events} == {"execution", "work_declaration"}
    work = next(e for e in events if e["event_kind"] == "work_declaration")
    assert work["payload"]["evaluation_suites"][0]["expected_case_ids"] == [
        task["decision"]["decision_id"]
    ]
    assert len(work["payload"]["child_task_ids"]) == 1
    assert "NEVER-EXPORT" not in json.dumps(events)
    assert telemetry().measurement_events(run, task, []) == events
    assert all(e["task_id"] == task["decision"]["task_id"] for e in events)


def test_review_before_note_is_ineligible_and_original_id_is_retained():
    run, task = bundles()
    task["reviews"] = [
        {
            "event_id": "source-review-id",
            "action_id": "action",
            "tenant_id": "tenant-a",
            "run_id": task["decision"]["run_id"],
            "task_id": task["decision"]["task_id"],
            "recommendation": None,
            "verdict": "approve",
            "reviewer_type": "human",
            "reviewed_at": "2026-09-30T10:03:00Z",
            "started_at": "2026-09-30T10:00:00Z",
        }
    ]
    review = next(
        e for e in telemetry().measurement_events(run, task, []) if e["event_kind"] == "review"
    )
    assert review["event_id"] == "source-review-id"
    assert review["payload"]["recommendation"] is None
    assert review["payload"]["agreement"] is None
    assert review["payload"]["reviewer_type"] == "human"
    assert review["payload"].get("started_at") == "2026-09-30T10:00:00Z"


def test_usage_maps_purpose_without_raw_request_or_response():
    run, task = bundles()
    task["calls"] = [
        {
            "call_id": "one",
            "provider": "anthropic",
            "purpose": "judge",
            "model": "pinned",
            "cost": "0.123456789012",
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "price_table_version": "price",
            "occurred_at": "2026-09-30T10:02:00Z",
            "request_document": {"secret": "NEVER-EXPORT"},
        }
    ]
    events = telemetry().measurement_events(run, task, [])
    call = next(e for e in events if e["event_kind"] == "provider_usage")
    assert call["payload"]["cost_scope"] == "offline"
    assert call["payload"]["cost_amount"] == "0.123456789012"
    assert call["payload"]["call_id"] == "one"
    assert "NEVER-EXPORT" not in json.dumps(events)
    unknown = deepcopy(task)
    unknown["calls"][0]["purpose"] = "unknown-purpose"
    with pytest.raises(ValueError, match="purpose"):
        telemetry().measurement_events(run, unknown, [])


def test_domain_contributions_and_reconciliation_use_exact_independent_values():
    run, task = bundles()
    api = telemetry()
    assert hasattr(api, "domain_outcome"), "workload outcome mapping missing"
    result = api.domain_outcome(
        task["decision"],
        label="legitimate",
        amount_minor=10000,
        threshold={"review_cost": "4", "margin_rate": ".30"},
    )
    assert result["correct"] is True
    assert result["review_cost"] == "4"
    assert result["error_cost"] == "0"
    assert result["rate_contributions"] == {
        "false_positive": {"numerator": 0, "denominator": 1},
        "missed_fraud": {"numerator": 0, "denominator": 0},
        "escalation": {"numerator": 1, "denominator": 1},
    }
    assert "label" not in result
    assert hasattr(api, "reconcile_report"), "exact reconciliation missing"
    expected = {
        "run_id": "local",
        "model_cost": "0.3",
        "correct_tasks": 1,
        "generation": "snapshot-one",
    }
    assert api.reconcile_report(expected, expected, expected)["matched"] is True
    assert (
        api.reconcile_report(expected, expected | {"model_cost": "0.300000000001"}, expected)[
            "matched"
        ]
        is False
    )


def test_telemetry_cli_exposes_no_provider_credentials():
    import subprocess

    result = subprocess.run(
        ["reckoner", "v1", "telemetry-export", "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "--endpoint" in result.stdout
    assert "--env-file" in result.stdout
    assert "--limit" in result.stdout


def test_otlp_provenance_and_genai_attributes_are_allowlisted():
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from reckoner.v1.telemetry.exporter import encode

    run, task = bundles()
    task["calls"] = [
        {
            "call_id": "call",
            "provider": "typesafe",
            "purpose": "final",
            "model": "pinned",
            "cost": None,
            "usage": {"input_tokens": -1, "output_tokens": True},
            "price_table_version": "price",
            "occurred_at": "2026-09-30T10:02:00Z",
        }
    ]
    doc = next(
        e
        for e in telemetry().measurement_events(run, task, [])
        if e["event_kind"] == "provider_usage"
    )
    assert doc["payload"]["input_tokens"] is None
    assert doc["payload"]["output_tokens"] is None
    span = (
        ExportTraceServiceRequest.FromString(encode(doc)).resource_spans[0].scope_spans[0].spans[0]
    )
    attrs = {a.key: a.value for a in span.attributes}
    assert attrs["touchstone.dataset_simulated"].bool_value is True
    assert attrs["touchstone.provider_call_mode"].string_value == "measured"
    assert attrs["gen_ai.provider.name"].string_value == "typesafe"
    assert attrs["gen_ai.request.model"].string_value == "pinned"
    assert "gen_ai.usage.input_tokens" not in attrs

from __future__ import annotations

import importlib
import json
from copy import deepcopy
from decimal import Decimal

import pytest


def _snapshot() -> dict:
    outcomes = [
        ("tenant-a", "auto-approve", "legitimate", True, "0", "0"),
        ("tenant-a", "auto-approve", "fraud", False, "0", "100"),
        ("tenant-a", "auto-decline", "legitimate", False, "0", "30"),
        ("tenant-b", "auto-decline", "fraud", True, "0", "0"),
        ("tenant-b", "escalate", "legitimate", True, "4", "0"),
        ("tenant-b", "escalate", "fraud", True, "4", "0"),
    ]
    tasks = []
    attempts = []
    evaluations = []
    for index, (tenant, outcome, label, correct, review, error) in enumerate(outcomes, 1):
        task_id = f"task-{index}"
        tasks.append(
            {
                "tenant_id": tenant,
                "task_id": task_id,
                "status": "completed",
                "label_class": label,
            }
        )
        attempts.append(
            {
                "tenant_id": tenant,
                "task_id": task_id,
                "call_id": f"call-{index}",
                "status": "responded",
                "actual_cost": Decimal("0.01"),
                "maximum_cost": Decimal("0.02"),
                "duration_ms": Decimal(index if index < 5 else 4),
            }
        )
        evaluations.append(
            {
                "tenant_id": tenant,
                "task_id": task_id,
                "outcome": outcome,
                "label_class": label,
                "document": {
                    "status": "observed",
                    "correct": correct,
                    "review_cost": review,
                    "error_cost": error,
                    "currency": "USD",
                    "schema_valid": True,
                    "required_suite_status": "pass" if correct else "fail",
                    "evaluation_errors": [],
                },
            }
        )
    return {
        "run": {
            "run_id": "six-case",
            "purpose": "baseline",
            "status": "complete",
            "execution_mode": "paid",
            "provider_call_mode": "measured",
            "config_id": "config-1",
            "bundle_id": "bundle-1",
            "prompt_version": "prompt-1",
            "model": "model-1",
            "price_table_version": "price-1",
            "code_revision": "code-1",
            "created_at": "2026-09-25T10:00:00+00:00",
            "preflight_at": "2026-09-25T10:01:00+00:00",
            "threshold_config_ids": {
                "tenant-a": "threshold-a",
                "tenant-b": "threshold-b",
            },
            "cohort_ids": {"tenant-a": "cohort-a", "tenant-b": "cohort-b"},
        },
        "tenants": ["tenant-a", "tenant-b"],
        "tasks": tasks,
        "attempts": attempts,
        "evaluations": evaluations,
        "exports": [
            {
                "producer": "runner",
                "status": "exported",
                "event_id": "runner-event-1",
                "task_id": "task-1",
            }
        ],
    }


def test_six_case_known_answer_uses_fixed_denominators_and_exact_decimal_cpst():
    build_report = importlib.import_module("reckoner.baseline.report").build_report

    report = build_report(_snapshot())
    metrics = report["aggregate"]["metrics"]

    assert report["schema_version"] == "reckoner-report-v1"
    assert metrics["correctness"] == {
        "numerator": 4,
        "denominator": 6,
        "value": "0.6666666666666666666666666667",
    }
    assert metrics["false_positive_rate"] == {
        "numerator": 1,
        "denominator": 3,
        "value": "0.3333333333333333333333333333",
    }
    assert metrics["missed_fraud_rate"] == {
        "numerator": 1,
        "denominator": 3,
        "value": "0.3333333333333333333333333333",
    }
    assert metrics["escalation_rate"] == {
        "numerator": 2,
        "denominator": 6,
        "value": "0.3333333333333333333333333333",
    }
    assert metrics["completion_rate"] == {"numerator": 6, "denominator": 6, "value": "1"}
    assert metrics["response_schema_validity"] == {
        "numerator": 6,
        "denominator": 6,
        "value": "1",
    }
    assert metrics["required_suite_pass_rate"] == {
        "numerator": 4,
        "denominator": 6,
        "value": "0.6666666666666666666666666667",
        "status": "fail",
    }
    assert metrics["costs"] == {
        "model": "0.06",
        "review": "8",
        "error": "130",
        "total": "138.06",
        "currency": "USD",
        "unknown_provider_costs": 0,
        "reserved_unsettled": "0",
    }
    assert metrics["cpst"] == {
        "numerator": "138.06",
        "denominator": 4,
        "value": "34.515",
        "currency": "USD",
        "availability": "available",
    }
    assert metrics["latency_ms"] == {"population": 6, "p99_nearest_rank": "4"}


def test_failed_seventh_case_stays_in_denominators_and_withholds_full_cpst():
    build_report = importlib.import_module("reckoner.baseline.report").build_report
    snapshot = _snapshot()
    snapshot["run"]["status"] = "incomplete"
    snapshot["tasks"].append(
        {
            "tenant_id": "tenant-a",
            "task_id": "task-7",
            "status": "failed",
            "label_class": "legitimate",
        }
    )
    snapshot["evaluations"].append(
        {
            "tenant_id": "tenant-a",
            "task_id": "task-7",
            "outcome": None,
            "label_class": "legitimate",
            "document": {
                "status": "failed",
                "correct": None,
                "review_cost": None,
                "error_cost": None,
                "currency": None,
                "schema_valid": False,
                "required_suite_status": "error",
                "evaluation_errors": ["missing_outcome"],
            },
        }
    )

    report = build_report(snapshot)
    metrics = report["aggregate"]["metrics"]

    assert metrics["correctness"]["denominator"] == 7
    assert metrics["false_positive_rate"]["denominator"] == 4
    assert metrics["completion_rate"] == {
        "numerator": 6,
        "denominator": 7,
        "value": "0.8571428571428571428571428571",
    }
    assert metrics["costs"]["review"] == "8"
    assert metrics["costs"]["error"] == "130"
    assert metrics["cpst"] == {
        "numerator": None,
        "denominator": 4,
        "value": None,
        "currency": "USD",
        "availability": "incomplete_outcomes",
    }
    assert report["counts"]["failed"] == 1


def test_missing_evaluation_never_shrinks_label_or_task_denominators():
    build_report = importlib.import_module("reckoner.baseline.report").build_report
    snapshot = _snapshot()
    snapshot["evaluations"] = snapshot["evaluations"][1:]

    report = build_report(snapshot)
    metrics = report["aggregate"]["metrics"]

    assert metrics["correctness"]["denominator"] == 6
    assert metrics["false_positive_rate"]["denominator"] == 3
    assert metrics["missed_fraud_rate"]["denominator"] == 3
    assert report["counts"]["evaluation_errors"] == 1
    assert report["run"]["status"] == "incomplete"


def test_unknown_cost_zero_populations_duplicate_calls_and_no_correct_outcomes_are_honest():
    build_report = importlib.import_module("reckoner.baseline.report").build_report
    snapshot = _snapshot()
    duplicate = deepcopy(snapshot["attempts"][0])
    snapshot["attempts"].append(duplicate)
    snapshot["attempts"][1]["actual_cost"] = None
    snapshot["attempts"][1]["status"] = "uncertain"
    snapshot["evaluations"] = [
        evaluation | {"document": evaluation["document"] | {"correct": False}}
        for evaluation in snapshot["evaluations"]
    ]
    snapshot["tenants"].append("tenant-empty")

    report = build_report(snapshot)
    aggregate = report["aggregate"]["metrics"]

    assert aggregate["costs"]["model"] == "0.05"
    assert aggregate["costs"]["unknown_provider_costs"] == 1
    assert aggregate["cpst"]["availability"] == "unknown_provider_cost"
    assert aggregate["cpst"]["denominator"] == 0
    assert report["run"]["status"] == "incomplete"
    assert report["per_tenant"]["tenant-empty"]["metrics"]["completion_rate"] == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
    }


def test_write_report_is_deterministic_and_markdown_marks_unavailable(tmp_path):
    report_module = importlib.import_module("reckoner.baseline.report")
    snapshot = _snapshot()
    snapshot["run"]["status"] = "incomplete"
    snapshot["attempts"][0]["actual_cost"] = None
    report = report_module.build_report(snapshot)
    output = tmp_path / "evidence"

    report_module.write_report(report, output)
    first_json = (output / "report.json").read_bytes()
    first_markdown = (output / "report.md").read_bytes()
    report_module.write_report(report, output)

    assert (output / "report.json").read_bytes() == first_json
    assert json.loads(first_json) == report
    assert (output / "report.md").read_bytes() == first_markdown
    markdown = first_markdown.decode()
    assert "Unavailable" in markdown
    for caveat in ("simulated", "enriched", "USD", "UTC", "ideal reviewer"):
        assert caveat.lower() in markdown.lower()


def test_pilot_report_states_the_pilot_enriched_mix():
    build_report = importlib.import_module("reckoner.baseline.report").build_report
    snapshot = _snapshot()
    snapshot["run"]["purpose"] = "pilot"

    report = build_report(snapshot)

    caveats = " ".join(report["caveats"])
    assert "2 fraud and 18 legitimate" in caveats
    assert "100 fraud and 900 legitimate" not in caveats


def test_report_distinguishes_fake_test_calls_from_paid_measured_calls():
    build_report = importlib.import_module("reckoner.baseline.report").build_report
    fake_snapshot = _snapshot()
    fake_snapshot["run"]["execution_mode"] = "test"
    fake_snapshot["run"]["provider_call_mode"] = "fake"

    fake_report = build_report(fake_snapshot)
    measured_report = build_report(_snapshot())

    assert fake_report["run"]["provider_call_mode"] == "fake"
    assert "explicit fake provider" in " ".join(fake_report["caveats"]).lower()
    assert measured_report["run"]["provider_call_mode"] == "measured"
    assert "external provider calls were measured" in " ".join(measured_report["caveats"]).lower()


def test_markdown_renders_complete_report_evidence(tmp_path):
    report_module = importlib.import_module("reckoner.baseline.report")
    snapshot = _snapshot()
    snapshot["tasks"][0]["status"] = "failed"
    snapshot["tasks"][1]["status"] = "uncertain"
    snapshot["evaluations"][0]["document"]["evaluation_errors"] = ["synthetic_error"]
    snapshot["attempts"][0]["actual_cost"] = None
    snapshot["attempts"][0]["status"] = "uncertain"
    report = report_module.build_report(snapshot)

    report_module.write_report(report, tmp_path)
    markdown = (tmp_path / "report.md").read_text()

    for required in (
        "## Run provenance",
        "Provider call mode: fake"
        if report["run"]["provider_call_mode"] == "fake"
        else "Provider call mode: measured",
        "## Counts",
        "Failed: 1",
        "Uncertain: 1",
        "False-positive rate",
        "Missed-fraud rate",
        "Escalation rate",
        "Response-schema validity",
        "Required-suite pass rate",
        "Reserved unsettled",
        "## Tenant tenant-a",
        "## Tenant tenant-b",
        "## Evaluation errors",
        "synthetic_error",
        "## Trace and export references",
        "runner-event-1",
    ):
        assert required in markdown


@pytest.mark.parametrize("invalid_money", ["01.00", "-1", "1e-2"])
def test_report_schema_rejects_invalid_money_strings(tmp_path, invalid_money):
    report_module = importlib.import_module("reckoner.baseline.report")
    report = report_module.build_report(_snapshot())
    report["aggregate"]["metrics"]["costs"]["model"] = invalid_money

    with pytest.raises(ValueError, match="invalid report document"):
        report_module.write_report(report, tmp_path)


def test_report_schema_rejects_unknown_fields(tmp_path):
    report_module = importlib.import_module("reckoner.baseline.report")
    report = report_module.build_report(_snapshot())
    report["unreviewed_extension"] = True

    with pytest.raises(ValueError, match="invalid report document"):
        report_module.write_report(report, tmp_path)

"""Independent acceptance receipt checks for the frozen measured baseline."""

import json
from pathlib import Path

from touchstone_platform.verify import compare_summary

EXPECTED = json.loads(
    (Path(__file__).resolve().parent / "fixtures/baseline-expected.json").read_text()
)


def _row(receipt, tenant_id=None):
    return {
        "tenant_id": tenant_id,
        "run_id": EXPECTED["run_id"],
        "measurement_mode": "measured",
        "dataset_simulated": True,
        "metrics_complete": True,
        "expected_tasks": receipt["expected"],
        "completed_tasks": receipt["completed"],
        "correct_tasks": receipt["correct"],
        "model_cost": receipt["model_cost"],
        "review_cost": receipt["review_cost"],
        "error_cost": receipt["error_cost"],
        "cpst": receipt["cpst"],
        "latency_p99_ms": float(receipt["p99_ms"]),
        "contribution_rates": [
            {
                "metric_id": metric,
                "numerator": pair[0],
                "denominator": pair[1],
                "rate": pair[0] / pair[1],
            }
            for metric, pair in (
                ("false_positive", receipt["false_positive"]),
                ("missed_fraud", receipt["missed_fraud"]),
                ("escalation", receipt["escalation"]),
            )
        ],
    }


def test_frozen_baseline_aggregate_and_tenants_reconcile():
    summary = _row(EXPECTED["aggregate"])
    summary["tenant_ids"] = ["tenant-a", "tenant-b"]
    summary["excluded_tenants"] = []
    summary["tenants"] = [_row(EXPECTED[tenant], tenant) for tenant in ("tenant-a", "tenant-b")]
    assert compare_summary(summary, EXPECTED) == []


def test_mismatched_money_rate_and_p99_fail_independently():
    summary = _row(EXPECTED["aggregate"])
    summary["tenant_ids"] = ["tenant-a", "tenant-b"]
    summary["excluded_tenants"] = []
    summary["tenants"] = [_row(EXPECTED[tenant], tenant) for tenant in ("tenant-a", "tenant-b")]
    summary["model_cost"] = "0.458941"
    summary["latency_p99_ms"] += 0.000002
    summary["contribution_rates"][0]["numerator"] = 15
    mismatches = compare_summary(summary, EXPECTED)
    assert any("model_cost" in mismatch for mismatch in mismatches)
    assert any("p99" in mismatch for mismatch in mismatches)
    assert any("false_positive" in mismatch for mismatch in mismatches)


def test_missing_tenant_and_incomplete_metrics_fail():
    summary = _row(EXPECTED["aggregate"])
    summary["tenant_ids"] = ["tenant-a"]
    summary["excluded_tenants"] = ["tenant-b"]
    summary["tenants"] = [_row(EXPECTED["tenant-a"], "tenant-a")]
    summary["metrics_complete"] = False
    mismatches = compare_summary(summary, EXPECTED)
    assert any("metrics_complete" in mismatch for mismatch in mismatches)
    assert any("tenant-b" in mismatch for mismatch in mismatches)


def test_changed_rate_fails_even_when_numerator_and_denominator_match():
    summary = _row(EXPECTED["aggregate"])
    summary["tenant_ids"] = ["tenant-a", "tenant-b"]
    summary["excluded_tenants"] = []
    summary["tenants"] = [_row(EXPECTED[tenant], tenant) for tenant in ("tenant-a", "tenant-b")]
    summary["contribution_rates"][0]["rate"] = 0.5
    assert "aggregate.false_positive.rate" in compare_summary(summary, EXPECTED)


def test_expected_total_cost_is_checked_separately():
    summary = _row(EXPECTED["aggregate"])
    summary["tenant_ids"] = ["tenant-a", "tenant-b"]
    summary["excluded_tenants"] = []
    summary["tenants"] = [_row(EXPECTED[tenant], tenant) for tenant in ("tenant-a", "tenant-b")]
    wrong_receipt = json.loads(json.dumps(EXPECTED))
    wrong_receipt["aggregate"]["total_cost"] = "7321.795941"
    assert "aggregate.total_cost" in compare_summary(summary, wrong_receipt)

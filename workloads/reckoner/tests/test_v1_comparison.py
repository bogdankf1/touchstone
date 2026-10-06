"""Prevent incompatible or incomplete arms from presenting a delta."""

from copy import deepcopy

import pytest


def arm():
    return {
        "arm_id": "baseline",
        "workflow_id": "reckoner",
        "cohort_id": "cohort",
        "case_ids": ["a", "b"],
        "tenant_assignment": {"a": "t", "b": "t"},
        "oracle_version": "oracle",
        "currency": "USD",
        "business_config": {"review": "4"},
        "config_version": "cfg",
        "model_version": "model",
        "prompt_version": "prompt",
        "question_version": "question",
        "calibration_id": "cal",
        "evidence_version": "relational",
        "calibration_binding": {
            "model_version": "model",
            "prompt_version": "prompt",
            "question_version": "question",
            "evidence_version": "relational",
            "retrieval_window": {"history_days": 30, "resolution_days": 90},
        },
        "retrieval_window": {"history_days": 30, "resolution_days": 90},
        "call_ids": ["call"],
        "execution_mode": "fabricated",
        "dataset_simulated": True,
        "generation": "g",
        "refresh_state": "succeeded",
        "metrics_complete": True,
        "online_cost_complete": True,
        "graph_coverage": "available",
        "correct_tasks": 2,
        "cpst": "2.5",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("case_ids", ["a"]),
        ("case_ids", ["a", "b", "extra"]),
        ("case_ids", ["a", "a"]),
        ("tenant_assignment", {"a": "other", "b": "t"}),
        ("currency", "EUR"),
        ("cohort_id", "other"),
        ("business_config", {"review": "5"}),
        ("generation", "partial"),
        ("refresh_state", "failed"),
        ("online_cost_complete", False),
        ("graph_coverage", "unavailable"),
        ("correct_tasks", 0),
        ("metrics_complete", False),
        ("oracle_version", "other-oracle"),
        ("dataset_simulated", False),
    ],
)
def test_ineligible_has_no_delta(field, value):
    from reckoner.v1.benchmark.compare import comparison_eligibility

    baseline = arm()
    current = deepcopy(baseline)
    current[field] = value
    result = comparison_eligibility(baseline, current)
    assert result["eligible"] is False
    assert result["delta_cpst"] is None
    assert result["reasons"]


def test_arms_preserve_treatments_and_reject_inherited_calibration():
    from reckoner.v1.benchmark.compare import compare_arms

    baseline = arm()
    current = deepcopy(baseline)
    current.update(arm_id="current", cpst="2", model_version="new", calibration_id="new-cal")
    with pytest.raises(ValueError, match="calibration"):
        compare_arms([baseline, current], ["a", "b"])
    current["calibration_binding"]["model_version"] = "new"
    result = compare_arms([baseline, current], ["a", "b"])
    assert result["comparisons"][0]["delta_cpst"] == "-0.5"
    assert [a["model_version"] for a in result["arms"]] == ["model", "new"]
    current["case_ids"] = ["a"]
    assert compare_arms([baseline, current], ["a", "b"])["comparisons"][0]["eligible"] is False


def test_compare_cli_writes_immutable_versioned_artifact_without_provider(tmp_path):
    import json
    from argparse import Namespace

    from reckoner.v1.cli import execute

    a = arm()
    b = deepcopy(a)
    b.update(arm_id="current", cpst="2")
    (tmp_path / "arms.json").write_text(json.dumps([a, b]))
    (tmp_path / "ids.json").write_text(json.dumps(["a", "b"]))
    output = tmp_path / "comparison"
    result = execute(
        Namespace(
            v1_command="compare",
            arms=tmp_path / "arms.json",
            expected=tmp_path / "ids.json",
            output=output,
            measurement_mode="synthetic-fixture",
        )
    )
    assert result["comparisons"][0]["delta_cpst"] == "-0.5"
    assert (
        json.loads(output.with_suffix(".json").read_text())["schema_version"]
        == "reckoner-comparison-v1"
    )
    markdown = output.with_suffix(".md").read_text()
    assert markdown.startswith("# Synthetic-fixture Reckoner v1 arm comparison\n")
    assert "plumbing only" in markdown
    assert "Query count" not in markdown
    assert "| baseline | current | eligible | none | -0.5 |" in markdown


def test_comparison_markdown_shows_ineligibility_reasons_without_delta(tmp_path):
    from reckoner.v1.benchmark.compare import compare_arms
    from reckoner.v1.benchmark.report import write_report

    a = arm()
    b = deepcopy(a)
    b.update(arm_id="current|<b>", currency="EUR")
    result = {**compare_arms([a, b], ["a", "b"]), "measurement_mode": "measured-comparison"}
    write_report(result, tmp_path / "comparison")
    markdown = (tmp_path / "comparison.md").read_text()
    assert (
        "| baseline | current\\|\\<b\\> | ineligible | incompatible currency | unavailable |"
        in (markdown)
    )
    assert "<b>" not in markdown.split("## Full record")[0]


def test_changed_input_cannot_reuse_same_calibration_identity():
    from reckoner.v1.benchmark.compare import compare_arms

    a = arm()
    b = deepcopy(a)
    b.update(arm_id="changed", prompt_version="new")
    b["calibration_binding"]["prompt_version"] = "new"
    with pytest.raises(ValueError, match="calibration"):
        compare_arms([a, b], ["a", "b"])


def test_changed_retrieval_window_cannot_reuse_same_calibration_identity():
    from reckoner.v1.benchmark.compare import compare_arms

    a = arm()
    b = deepcopy(a)
    window = {"history_days": 60, "resolution_days": 90}
    b.update(arm_id="changed", retrieval_window=window)
    b["calibration_binding"]["retrieval_window"] = window
    with pytest.raises(ValueError, match="calibration"):
        compare_arms([a, b], ["a", "b"])


def test_compare_arms_rejects_duplicate_expected_ids():
    from reckoner.v1.benchmark.compare import compare_arms

    with pytest.raises(ValueError, match="invalid declared cases"):
        compare_arms([arm()], ["a", "a"])


def test_comparison_report_requires_measurement_label(tmp_path):
    from reckoner.v1.benchmark.compare import compare_arms
    from reckoner.v1.benchmark.report import write_report

    with pytest.raises(ValueError, match="measurement_mode"):
        write_report(compare_arms([arm()], ["a", "b"]), tmp_path / "comparison")
    assert list(tmp_path.iterdir()) == []
    measured = {**compare_arms([arm()], ["a", "b"]), "measurement_mode": "measured-comparison"}
    write_report(measured, tmp_path / "comparison")
    markdown = (tmp_path / "comparison.md").read_text()
    assert markdown.startswith("# Measured Reckoner v1 arm comparison\n")
    assert "plumbing only" not in markdown


def test_cpst_delta_keeps_full_decimal_precision():
    from reckoner.v1.benchmark.compare import comparison_eligibility

    baseline = arm()
    current = deepcopy(baseline)
    baseline["cpst"] = "0.000000000000000000000000000001"
    current["cpst"] = "1234567890.123456789012345678901234"
    delta = comparison_eligibility(baseline, current)["delta_cpst"]
    assert delta == "1234567890.123456789012345678901233999999"


def test_missing_calibration_ids_are_missing_provenance_not_reuse():
    from reckoner.v1.benchmark.compare import compare_arms

    a = arm()
    b = deepcopy(a)
    b.update(arm_id="current", model_version="other")
    for item in (a, b):
        item["calibration_id"] = None
        item.pop("calibration_binding")
    result = compare_arms([a, b], ["a", "b"])
    assert result["comparisons"][0]["eligible"] is False
    assert "missing arm provenance" in result["comparisons"][0]["reasons"]


def test_workload_and_platform_eligibility_rules_correspond():
    from reckoner.v1.benchmark import compare
    from touchstone_platform import comparison

    # Workload arm field -> generic platform field with the same meaning.
    names = {
        "workflow_id": "workflow_id",
        "cohort_id": "cohort_version",
        "oracle_version": "reference_version",
        "business_config": "business_config_id",
        "currency": "currency",
        "execution_mode": "measurement_mode",
        "dataset_simulated": "dataset_simulated",
        "generation": "generation",
    }
    # The platform carries tenant assignment inside its (tenant, task) case membership.
    workload_only = {"tenant_assignment"}
    assert set(compare.INVARIANTS) - workload_only == set(names)
    assert {names[k] for k in compare.INVARIANTS if k in names} == set(comparison.INVARIANTS)
    assert compare.BINDING == comparison.BINDING
    assert set(compare.PINNED) == set(comparison.PINS)

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
        },
        "retrieval_window": {"history_days": 30, "resolution_days": 90},
        "call_ids": ["call"],
        "execution_mode": "fabricated",
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
        )
    )
    assert result["comparisons"][0]["delta_cpst"] == "-0.5"
    assert (
        json.loads(output.with_suffix(".json").read_text())["schema_version"]
        == "reckoner-comparison-v1"
    )


def test_changed_input_cannot_reuse_same_calibration_identity():
    from reckoner.v1.benchmark.compare import compare_arms

    a = arm()
    b = deepcopy(a)
    b.update(arm_id="changed", prompt_version="new")
    b["calibration_binding"]["prompt_version"] = "new"
    with pytest.raises(ValueError, match="calibration"):
        compare_arms([a, b], ["a", "b"])

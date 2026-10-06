"""Strict v1 contracts reject drift and preserve decimal probabilities."""

import importlib
from copy import deepcopy

import pytest
from v1_fixtures import (
    config_fixture,
    decision_fixture,
    evidence_fixture,
    experiment_fixture,
    identified,
    note_fixture,
    resolution_fixture,
    review_fixture,
    score_fixture,
)


def validate(kind, document):
    try:
        module = importlib.import_module("reckoner.v1.contracts")
    except ModuleNotFoundError:
        pytest.fail("strict Reckoner v1 validation is not implemented")
    return module.validate_v1(kind, document)


BUILDERS = {
    "run-config": config_fixture,
    "evidence": evidence_fixture,
    "score": score_fixture,
    "decision": decision_fixture,
    "case-note": note_fixture,
    "review": review_fixture,
    "resolution": resolution_fixture,
    "experiment": experiment_fixture,
}


@pytest.mark.parametrize("kind", BUILDERS)
def test_valid_documents_are_detached_and_unknown_keys_and_missing_tenant_fail(kind):
    document = BUILDERS[kind]()
    result = validate(kind, document)
    assert result == document and result is not document
    result["tenant_id"] = "changed"
    assert document["tenant_id"] == "tenant-a"
    nested = next((key for key, value in result.items() if isinstance(value, dict | list)), None)
    if nested is not None:
        result[nested].clear()
        assert document[nested]
    for changed in (
        {**document, "unknown": "value"},
        {key: value for key, value in document.items() if key != "tenant_id"},
    ):
        with pytest.raises(ValueError):
            validate(kind, changed)


def test_binary_decimal_distribution_is_preserved_and_must_sum_to_one():
    assert (
        validate("score", score_fixture(fraud="0.2", legitimate="0.8"))["raw_probability"] == "0.2"
    )
    with pytest.raises(ValueError):
        validate("score", score_fixture(fraud="0.2", legitimate="0.7"))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1.01", "-0.1", float("nan")])
def test_nonfinite_or_out_of_range_probabilities_fail(value):
    with pytest.raises(ValueError):
        validate("score", score_fixture(fraud=value))


def test_degraded_decision_can_have_no_call_or_probability_but_success_cannot():
    result = validate("decision", decision_fixture(degraded=True))
    assert result["call_id"] is None and result["raw_probability"] is None
    invalid = decision_fixture()
    invalid.update(call_id=None, raw_probability=None)
    identified(invalid, "decision_id")
    with pytest.raises(ValueError):
        validate("decision", invalid)


def test_config_hash_and_model_price_combination_are_validated():
    invalid = config_fixture()
    invalid["feature_version"] = "mutated"
    with pytest.raises(ValueError):
        validate("run-config", invalid)
    invalid = config_fixture()
    invalid["scorer"]["price_table"]["model"] = "wrong-model"
    identified(invalid["scorer"]["price_table"], "price_table_version")
    identified(invalid, "config_id")
    with pytest.raises(ValueError):
        validate("run-config", invalid)


def test_nested_unknown_fields_and_naive_timing_fail():
    invalid = note_fixture()
    invalid["confidence"]["fictional"] = True
    identified(invalid, "note_id")
    with pytest.raises(ValueError):
        validate("case-note", invalid)
    invalid = decision_fixture()
    invalid["completed_at"] = "2019-01-08T00:00:01"
    identified(invalid, "decision_id")
    with pytest.raises(ValueError):
        validate("decision", invalid)


def test_semantic_inconsistencies_fail_even_with_valid_hashes():
    cases = []
    score = score_fixture()
    score["raw_probability"] = "0.3"
    cases.append(("score", score, None))
    config = config_fixture()
    config["score_mode"] = "calibrated"
    cases.append(("run-config", config, "config_id"))
    decision = decision_fixture(degraded=True)
    decision["outcome"] = "auto-approve"
    cases.append(("decision", decision, "decision_id"))
    evidence = evidence_fixture()
    evidence["comparable_cases"] = [
        {
            "tenant_id": "tenant-a",
            "transaction_id": "past",
            "verdict": "approve",
            "resolved_at": evidence["query_time"],
            "resolution_policy_version": "seven-days",
            "similarity": "0.8",
            "evidence_refs": ["ref-a"],
        }
    ]
    cases.append(("evidence", evidence, "evidence_id"))
    experiment = experiment_fixture()
    experiment["tasks"].append(deepcopy(experiment["tasks"][0]))
    cases.append(("experiment", experiment, "experiment_id"))
    for kind, document, key in cases:
        if key:
            identified(document, key)
        with pytest.raises(ValueError):
            validate(kind, document)


def test_long_decimal_distribution_is_not_rounded_into_a_valid_sum():
    with pytest.raises(ValueError):
        validate("score", score_fixture(fraud="0.2", legitimate="0.80000100000000000000000000001"))


@pytest.mark.parametrize(
    "probability,outcome",
    [
        ("0.049", "auto-approve"),
        ("0.05", "escalate"),
        ("0.90", "escalate"),
        ("0.901", "auto-decline"),
    ],
)
def test_successful_decision_agrees_with_strict_threshold_boundaries(probability, outcome):
    decision = decision_fixture()
    decision.update(raw_probability=probability, effective_probability=probability, outcome=outcome)
    identified(decision, "decision_id")
    assert validate("decision", decision)["outcome"] == outcome
    decision["outcome"] = "auto-decline" if outcome != "auto-decline" else "auto-approve"
    identified(decision, "decision_id")
    with pytest.raises(ValueError):
        validate("decision", decision)


def test_projection_content_identity_rejects_changed_body_with_valid_evidence_hash():
    from evidence_fixtures import records
    from reckoner.contracts import content_id
    from reckoner.v1.evidence.assemble import document
    from reckoner.v1.evidence.neo4j import PARAMETERS

    query, _, _ = records()
    receipt = {
        "cutoff": "2018-06-01T00:00:00Z",
        "window_days": 30,
        "gds_version": "2026.09.0",
        "algorithm": "louvain-page-rank-v1",
        "parameters": PARAMETERS,
        "node_count": 4,
        "edge_count": 6,
        "covered_accounts": 1,
        "covered_cards": 1,
        "covered_merchants": 2,
        "build_seconds": 1.0,
        "page_rank_converged": True,
        "page_rank_iterations": 3,
        "community_count": 1,
        "cross_tenant": True,
    }
    receipt["projection_id"] = content_id(receipt)
    receipt["snapshot_age_seconds"] = "43200.0"
    valid = document(
        query, "a" * 64, {"status": "available", "missing": []}, {}, projection=receipt
    )
    for key, value in [("page_rank_converged", False), ("node_count", 5)]:
        changed = deepcopy(valid)
        changed["graph_projection"][key] = value
        identified(changed, "evidence_id")
        with pytest.raises(ValueError, match="projection.*identity"):
            validate("evidence", changed)

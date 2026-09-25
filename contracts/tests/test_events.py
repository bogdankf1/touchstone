import json
from pathlib import Path

import pytest
from jsonschema import ValidationError
from reckoner.contracts import content_id, validate_document

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "schemas"
EXAMPLES = ROOT / "contracts" / "examples"


def load_example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def test_canonical_transaction_accepts_simulated_fixture():
    event = load_example("transaction-v1.json")
    validate_document(event, SCHEMAS / "transaction-v1.schema.json")


@pytest.mark.parametrize("change", ["missing_tenant", "oracle", "naive_time"])
def test_runtime_event_rejects_leakage_and_missing_context(change):
    event = load_example("transaction-v1.json")
    if change == "missing_tenant":
        event.pop("tenant_id")
    elif change == "oracle":
        event["label"] = "fraud"
    else:
        event["occurred_at"] = "2020-01-01T12:30:00"
    with pytest.raises(ValidationError):
        validate_document(event, SCHEMAS / "transaction-v1.schema.json")


def test_source_positions_distinguish_identical_looking_transactions():
    first = {"source_sha256": "a" * 64, "source_record": 1}
    second = {"source_sha256": "a" * 64, "source_record": 2}
    assert content_id(first) != content_id(second)
    assert content_id(first) == content_id(dict(reversed(list(first.items()))))


@pytest.mark.parametrize("change", ["float_amount", "missing_version", "not_simulated"])
def test_transaction_rejects_invalid_contract_values(change):
    event = load_example("transaction-v1.json")
    if change == "float_amount":
        event["amount_minor"] = 1200.5
    elif change == "missing_version":
        event.pop("schema_version")
    else:
        event["provenance"]["simulated"] = False
    with pytest.raises(ValidationError):
        validate_document(event, SCHEMAS / "transaction-v1.schema.json")


def test_content_id_rejects_non_finite_numbers():
    with pytest.raises(ValueError):
        content_id({"score": float("nan")})


@pytest.mark.parametrize("workflow_id", ["reckoner", "synthetic-document-router"])
def test_measurement_accepts_independent_workflows(workflow_id):
    event = load_example("measurement-v1.json")
    event["workflow_id"] = workflow_id
    validate_document(event, SCHEMAS / "measurement-v1.schema.json")


def test_pending_outcome_accepts_nullable_correctness():
    event = load_example("measurement-v1.json")
    event["event_kind"] = "outcome"
    event["payload"] = {
        "status": "pending",
        "correct": None,
        "review_cost": None,
        "error_cost": None,
        "outcome_version": "outcome-v1",
        "evaluator_version": None,
    }
    validate_document(event, SCHEMAS / "measurement-v1.schema.json")


def test_unavailable_provider_cost_requires_null_amount():
    event = load_example("measurement-v1.json")
    event["event_kind"] = "provider_usage"
    event["payload"] = {
        "provider": "example-provider",
        "model": "example-model",
        "call_id": "call-1",
        "input_tokens": 10,
        "output_tokens": 2,
        "cached_tokens": 0,
        "cost_status": "unavailable",
        "cost_amount": None,
        "currency": None,
        "price_table_version": None,
    }
    validate_document(event, SCHEMAS / "measurement-v1.schema.json")

    event["payload"]["cost_amount"] = "0.00"
    with pytest.raises(ValidationError):
        validate_document(event, SCHEMAS / "measurement-v1.schema.json")


def test_metric_contribution_has_explicit_numerator_and_denominator():
    event = load_example("measurement-v1.json")
    event["event_kind"] = "metric_contribution"
    event["payload"] = {
        "metric_id": "document.accepted",
        "numerator": 1,
        "denominator": 1,
        "unit": "count",
        "definition_version": "v1",
        "cost_component_id": None,
    }
    validate_document(event, SCHEMAS / "measurement-v1.schema.json")


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("event_kind", "fraud_decision"),
        ("missing_tenant", None),
        ("negative_tokens", -1),
        ("negative_duration", -0.1),
    ],
)
def test_measurement_rejects_invalid_generic_values(change, value):
    event = load_example("measurement-v1.json")
    if change == "event_kind":
        event["event_kind"] = value
    elif change == "missing_tenant":
        event.pop("tenant_id")
    elif change == "negative_tokens":
        event["event_kind"] = "provider_usage"
        event["payload"] = {
            "provider": "example-provider",
            "model": "example-model",
            "call_id": "call-1",
            "input_tokens": value,
            "output_tokens": 0,
            "cached_tokens": 0,
            "cost_status": "unavailable",
            "cost_amount": None,
            "currency": None,
            "price_table_version": None,
        }
    else:
        event["payload"]["duration_ms"] = value
    with pytest.raises(ValidationError):
        validate_document(event, SCHEMAS / "measurement-v1.schema.json")


def test_measurement_rejects_domain_or_oracle_fields():
    event = load_example("measurement-v1.json")
    event["label"] = "fraud"
    with pytest.raises(ValidationError):
        validate_document(event, SCHEMAS / "measurement-v1.schema.json")

    event = load_example("measurement-v1.json")
    event["payload"]["fraud_probability"] = 0.5
    with pytest.raises(ValidationError):
        validate_document(event, SCHEMAS / "measurement-v1.schema.json")

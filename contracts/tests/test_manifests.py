import json
from pathlib import Path

import pytest
from jsonschema import ValidationError
from reckoner import contracts

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "schemas"
EXAMPLES = ROOT / "contracts" / "examples"


def load_example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def refresh_identity(document: dict, identity_field: str) -> None:
    document[identity_field] = contracts.content_id(
        {key: value for key, value in document.items() if key != identity_field}
    )


def validate_fixture_union(manifests: list[dict]) -> None:
    transaction_ids = [
        transaction_id
        for manifest in manifests
        for transaction_id in manifest["selected_transaction_ids"]
    ]
    if len(transaction_ids) != len(set(transaction_ids)):
        raise ValueError("duplicate selected transaction IDs across tenant manifests")


def test_config_identity_changes_when_business_assumption_changes():
    original = {"review_cost": "4.00", "margin_rate": "0.30", "tenant_id": "a"}
    changed = {**original, "review_cost": "5.00"}
    assert contracts.content_id(original) != contracts.content_id(changed)


def test_default_threshold_config_is_valid():
    document = load_example("threshold-config-v1.json")
    contracts.validate_threshold_config(document, SCHEMAS / "threshold-config-v1.schema.json")


def test_rejects_stale_config_identity_after_parameter_edit():
    document = load_example("threshold-config-v1.json")
    document["parameters"]["review_cost"] = "5.00"
    with pytest.raises(ValueError, match="config identity"):
        contracts.validate_threshold_config(document, SCHEMAS / "threshold-config-v1.schema.json")


def test_rejects_inverted_thresholds_even_with_fresh_identity():
    document = load_example("threshold-config-v1.json")
    document["parameters"]["t_low_floor"] = "0.95"
    refresh_identity(document, "config_id")
    with pytest.raises(ValueError, match="threshold"):
        contracts.validate_threshold_config(document, SCHEMAS / "threshold-config-v1.schema.json")


def test_flat_config_rejects_high_threshold_at_or_below_flat_low_threshold():
    document = load_example("threshold-config-v1.json")
    document["parameters"]["amount_aware"] = False
    document["parameters"]["t_low_ceiling"] = "0.04"
    document["parameters"]["t_high"] = "0.05"
    refresh_identity(document, "config_id")
    with pytest.raises(ValueError, match="flat threshold"):
        contracts.validate_threshold_config(document, SCHEMAS / "threshold-config-v1.schema.json")


def test_fixture_cohort_manifest_is_valid():
    document = load_example("cohort-manifest-v1.json")
    contracts.validate_cohort_manifest(document, SCHEMAS / "cohort-manifest-v1.schema.json")


def test_manifest_accepts_complete_interval_status_partition():
    document = load_example("cohort-manifest-v1.json")
    document["counts"].update({"interval_source": 1, "invalid": 0})
    refresh_identity(document, "manifest_id")

    contracts.validate_cohort_manifest(document, SCHEMAS / "cohort-manifest-v1.schema.json")


def test_manifest_rejects_incomplete_interval_status_partition():
    document = load_example("cohort-manifest-v1.json")
    document["counts"].update({"interval_source": 2, "invalid": 0})
    refresh_identity(document, "manifest_id")

    with pytest.raises(ValueError, match="interval counts"):
        contracts.validate_cohort_manifest(document, SCHEMAS / "cohort-manifest-v1.schema.json")


def test_manifest_rejects_stale_identity_after_selection_edit():
    document = load_example("cohort-manifest-v1.json")
    document["seed"] += 1
    with pytest.raises(ValueError, match="manifest identity"):
        contracts.validate_cohort_manifest(document, SCHEMAS / "cohort-manifest-v1.schema.json")


def test_manifest_rejects_history_overlapping_holdout():
    document = load_example("cohort-manifest-v1.json")
    document["history_end"] = document["evaluation_start"]
    refresh_identity(document, "manifest_id")
    with pytest.raises(ValueError, match="temporal"):
        contracts.validate_cohort_manifest(document, SCHEMAS / "cohort-manifest-v1.schema.json")


def test_manifest_rejects_missing_source_checksums():
    document = load_example("cohort-manifest-v1.json")
    document.pop("source_hashes")
    refresh_identity(document, "manifest_id")
    with pytest.raises(ValidationError):
        contracts.validate_cohort_manifest(document, SCHEMAS / "cohort-manifest-v1.schema.json")


def test_manifest_rejects_class_counts_that_do_not_sum_to_total():
    document = load_example("cohort-manifest-v1.json")
    document["counts"]["fraud"] = 1
    refresh_identity(document, "manifest_id")
    with pytest.raises(ValueError, match="class counts"):
        contracts.validate_cohort_manifest(document, SCHEMAS / "cohort-manifest-v1.schema.json")


def test_manifest_rejects_selected_id_count_that_differs_from_total():
    document = load_example("cohort-manifest-v1.json")
    document["counts"].update({"total": 2, "fraud": 0, "legitimate": 2})
    refresh_identity(document, "manifest_id")
    with pytest.raises(ValueError, match="selected IDs"):
        contracts.validate_cohort_manifest(document, SCHEMAS / "cohort-manifest-v1.schema.json")


def test_fixture_union_rejects_duplicate_transaction_ids_across_tenants():
    first = load_example("cohort-manifest-v1.json")
    second = {**first, "tenant_id": "tenant-fixture-b"}
    with pytest.raises(ValueError, match="duplicate selected transaction IDs"):
        validate_fixture_union([first, second])

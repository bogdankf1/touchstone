import json
from pathlib import Path

from touchstone_platform.contracts import (
    validate_declaration,
    validate_event,
    validate_event_context,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"


def test_validated_event_preserves_identity_and_receipt_time():
    event = json.loads((EXAMPLES / "measurement-v1.json").read_text())
    validated = validate_event(event, "2026-09-26T10:00:00Z")
    assert (validated.tenant_id, validated.workflow_id, validated.run_id) == (
        "tenant-fixture-a",
        "reckoner",
        "run-fixture-1",
    )
    assert validated.received_at == "2026-09-26T10:00:00Z"
    assert validated.document["event_id"] == "measurement-fixture-1"


def test_declaration_example_is_valid():
    declaration = json.loads((EXAMPLES / "run-declaration-v1.json").read_text())
    assert validate_declaration(declaration)["dataset_simulated"] is True


def test_validated_event_document_mutation_cannot_change_identity_or_context():
    event = json.loads((EXAMPLES / "measurement-v1.json").read_text())
    validated = validate_event(event, "2026-09-26T10:00:00Z")
    original_hash = validated.content_sha256
    context = {
        "touchstone.tenant_id": "tenant-fixture-a",
        "touchstone.workflow_id": "reckoner",
        "touchstone.workflow_version": "fixture-v1",
        "touchstone.run_id": "run-fixture-1",
        "touchstone.task_id": "task-fixture-1",
        "touchstone.simulated": True,
    }

    event["tenant_id"] = "mutated-input"
    event["reproducibility"]["config_version"] = "mutated-input"
    returned = validated.document
    returned["tenant_id"] = "mutated-return"
    returned["reproducibility"]["config_version"] = "mutated-return"

    assert validated.document["tenant_id"] == "tenant-fixture-a"
    assert validated.document["reproducibility"]["config_version"] == "fixture-config-v1"
    assert validated.tenant_id == "tenant-fixture-a"
    assert validated.content_sha256 == original_hash
    validate_event_context(validated, context, "trace-fixture-1", "span-fixture-1")

import json
from pathlib import Path

from touchstone_platform.contracts import validate_declaration, validate_event

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"


def test_validated_event_preserves_identity_and_receipt_time():
    event = json.loads((EXAMPLES / "measurement-v1.json").read_text())
    validated = validate_event(event, "2026-09-26T10:00:00Z")
    assert (validated.tenant_id, validated.workflow_id, validated.run_id) == (
        "tenant-fixture-a", "reckoner", "run-fixture-1"
    )
    assert validated.received_at == "2026-09-26T10:00:00Z"
    assert validated.document["event_id"] == "measurement-fixture-1"


def test_declaration_example_is_valid():
    declaration = json.loads((EXAMPLES / "run-declaration-v1.json").read_text())
    assert validate_declaration(declaration)["dataset_simulated"] is True

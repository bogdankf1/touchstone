import copy
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError
from touchstone_platform.contracts import (
    declarations_conflict,
    validate_declaration,
    validate_event,
    validate_event_context,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _declaration():
    return json.loads((EXAMPLES / "run-declaration-v1.json").read_text())


def _measurement():
    return json.loads((EXAMPLES / "measurement-v1.json").read_text())


def test_declaration_count_matches_unique_task_ids():
    declaration = _declaration()
    declaration["expected_task_ids"] = ["a", "b"]
    declaration["expected_task_count"] = 2
    assert validate_declaration(declaration)["expected_task_count"] == 2

    for ids, count in [(["a", "a"], 2), (["a", "b"], 3)]:
        invalid = copy.deepcopy(declaration)
        invalid["expected_task_ids"] = ids
        invalid["expected_task_count"] = count
        with pytest.raises(ValidationError):
            validate_declaration(invalid)


def test_conflicting_declarations():
    original = _declaration()
    identical = copy.deepcopy(original)
    assert not declarations_conflict(original, identical)
    revised = copy.deepcopy(original)
    revised["expected_task_ids"] = ["a", "c"]
    assert declarations_conflict(original, revised)


def test_measurement_envelope_context_matches_span():
    event = _measurement()
    context = {
        "touchstone.tenant_id": event["tenant_id"],
        "touchstone.workflow_id": event["workflow_id"],
        "touchstone.workflow_version": event["workflow_version"],
        "touchstone.run_id": event["run_id"],
        "touchstone.task_id": event["task_id"],
        "touchstone.simulated": event["simulated"],
    }
    validated = validate_event(event, "2026-09-26T10:00:00Z")
    validate_event_context(validated, context, event["trace_id"], event["span_id"])
    bad_context = dict(context, **{"touchstone.tenant_id": "another-tenant"})
    with pytest.raises(ValueError, match="tenant_id"):
        validate_event_context(validated, bad_context, event["trace_id"], event["span_id"])
    with pytest.raises(ValueError, match="span_id"):
        validate_event_context(validated, context, event["trace_id"], "another-span")


def test_unknown_schema_is_rejected():
    event = _measurement()
    event["schema_version"] = "measurement-v2"
    with pytest.raises(ValidationError):
        validate_event(event, "2026-09-26T10:00:00Z")
    declaration = _declaration()
    declaration["schema_version"] = "run-declaration-v2"
    with pytest.raises(ValidationError):
        validate_declaration(declaration)


def test_tenant_identity_is_not_a_path():
    declaration = _declaration()
    del declaration["tenant_id"]
    with pytest.raises(ValidationError):
        validate_declaration(declaration)
    event = _measurement()
    del event["tenant_id"]
    with pytest.raises(ValidationError):
        validate_event(event, "2026-09-26T10:00:00Z")

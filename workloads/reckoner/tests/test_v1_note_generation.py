"""Fabricated notes verify software boundaries, never measured note quality."""

import importlib
import json

import pytest
from v1_fixtures import (
    config_fixture,
    decision_fixture,
    evidence_fixture,
    identified,
    note_fixture,
    score_fixture,
)


def api():
    try:
        return importlib.import_module("reckoner.v1.notes")
    except ModuleNotFoundError:
        pytest.fail("structured note generation is not implemented")


def sample():
    evidence = evidence_fixture()
    score = score_fixture()
    note = note_fixture()
    note["evidence_id"] = evidence["evidence_id"]
    note["confidence"] = {"value": score["confidence"], "meaning": "jev_distribution_concentration"}
    note["entity_neighbourhood"] = {
        "summary": "Neighbourhood unavailable.",
        "evidence_refs": [evidence["evidence_id"]],
    }
    note["what_would_change_verdict"][0]["evidence_refs"] = [evidence["evidence_id"]]
    identified(note, "note_id")
    return note, evidence, score


def test_exact_content_confidence_and_explicit_empty_evidence():
    note, evidence, score = sample()
    assert api().validate_note(note, evidence, score) == note
    assert note["confidence"]["value"] != score["raw_probability"]


@pytest.mark.parametrize(
    "change",
    ["confidence", "meaning", "unknown", "verdict", "reference", "unsupported", "ranks", "status"],
)
def test_rejects_invalid_notes(change):
    note, evidence, score = sample()
    if change == "confidence":
        note["confidence"]["value"] = score["raw_probability"]
    if change == "meaning":
        note["confidence"]["meaning"] = "raw_choice_probability"
    if change == "unknown":
        note["oracle"] = "fraud"
    if change == "verdict":
        note["verdict_recommendation"] = "escalate"
    if change == "reference":
        note["entity_neighbourhood"]["evidence_refs"] = ["invented"]
    if change == "unsupported":
        note["entity_neighbourhood"]["summary"] = "Four fraudulent neighbours."
    if change == "ranks":
        note["risk_indicators"] = [
            {
                "rank": 2,
                "indicator_id": "fake",
                "description": "fake",
                "method": "attribution",
                "evidence_refs": [evidence["evidence_id"]],
            }
        ]
    if change == "status":
        note["generation_status"] = "failed"
    identified(note, "note_id")
    with pytest.raises(ValueError):
        api().validate_note(note, evidence, score)


def test_absent_score_cannot_invent_confidence():
    note, evidence, _ = sample()
    with pytest.raises(ValueError):
        api().validate_note(note, evidence, {})
    note["confidence"] = {"value": None, "meaning": "unavailable"}
    note["generation_status"] = "degraded"
    identified(note, "note_id")
    assert api().validate_note(note, evidence, {}) == note


def test_prompt_allowlist_and_data_boundary():
    note, evidence, score = sample()
    case = decision_fixture(evidence=evidence)
    case["oracle_label"] = "SECRET-ORACLE"
    case["card_number"] = "SECRET-CARD"
    evidence["extraneous"] = "SECRET-RAW"
    score["api_key"] = "SECRET-KEY"
    request = api().build_note_request(case, evidence, score, config_fixture())
    serial = json.dumps(request)
    assert not any(
        value in serial for value in ["SECRET-ORACLE", "SECRET-CARD", "SECRET-RAW", "SECRET-KEY"]
    )
    assert request["model"] == config_fixture()["note_model"]["model"]
    assert "note-v1" in request["system"]
    assert evidence["evidence_id"] not in request["system"]


class Calls:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.stages = []

    def execute(self, request, *, stage, protocol):
        self.stages.append(stage)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return {
            "call_id": stage,
            "status": "responded",
            "billing_status": "settled",
            "body": response,
        }


def body(content):
    return {
        "content": content,
        "finish_reason": "stop",
        "reported_model": "claude-haiku-4-5-20251001",
    }


def test_generation_parses_content_and_repairs_at_most_once():
    note, evidence, score = sample()
    fields = {key: note[key] for key in api().CONTENT_FIELDS}
    calls = Calls([body("not json"), body(json.dumps(fields))])
    result = api().generate_note(
        decision_fixture(evidence=evidence),
        evidence,
        score,
        calls,
        config_fixture(),
        protocol={"approved": True, "maximum_attempts": 2},
    )
    assert result["status"] == "succeeded"
    assert result["note"]["confidence"] == note["confidence"]
    assert calls.stages == ["note-generation", "note-repair"]
    assert len(result["attempts"]) == 2


@pytest.mark.parametrize("content", ["", "null", '{"unknown":1}', '{"a":1,"a":2}'])
def test_invalid_content_exhausts_bounded_repair(content):
    _, evidence, score = sample()
    calls = Calls([body(content), body(content), body(content)])
    result = api().generate_note(
        decision_fixture(evidence=evidence),
        evidence,
        score,
        calls,
        config_fixture(),
        protocol={"approved": True, "maximum_attempts": 2},
    )
    assert result["status"] == "invalid"
    assert len(calls.stages) == 2


def test_missing_authorization_does_not_dispatch():
    _, evidence, score = sample()
    calls = Calls([])
    result = api().generate_note(
        decision_fixture(evidence=evidence), evidence, score, calls, config_fixture()
    )
    assert result["status"] == "pending"
    assert calls.stages == []


def test_refusal_is_not_schema_repair():
    _, evidence, score = sample()
    calls = Calls([{"content": "refused", "finish_reason": "refusal"}])
    result = api().generate_note(
        decision_fixture(evidence=evidence),
        evidence,
        score,
        calls,
        config_fixture(),
        protocol={"approved": True, "maximum_attempts": 2},
    )
    assert result["status"] == "failed"
    assert len(calls.stages) == 1


def test_actual_litellm_note_transport_has_pinned_model_bound_and_no_retry():
    import httpx
    from test_provider import anthropic_response

    module = importlib.import_module("reckoner.v1.notes.provider")
    _, evidence, score = sample()
    request = api().build_note_request(
        decision_fixture(evidence=evidence), evidence, score, config_fixture()
    )
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=anthropic_response())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = module.NoteProvider("fabricated-only", _http_client=http).generate(request)
    assert len(seen) == 1
    assert json.loads(seen[0].content)["max_tokens"] == 1000
    assert result["input_tokens"] == 31 and result["output_tokens"] == 7


@pytest.mark.parametrize("status", [401, 400, 429, 500])
def test_note_transport_does_not_retry_provider_errors(status):
    import httpx

    module = importlib.import_module("reckoner.v1.notes.provider")
    _, evidence, score = sample()
    request = api().build_note_request(
        decision_fixture(evidence=evidence), evidence, score, config_fixture()
    )
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            status, json={"type": "error", "error": {"type": "api_error", "message": "fabricated"}}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        from reckoner.baseline.provider import ProviderError

        with pytest.raises(ProviderError):
            module.NoteProvider("fabricated-only", _http_client=http).generate(request)
    assert len(seen) == 1


def test_confidence_from_another_tenant_is_rejected():
    note, evidence, score = sample()
    score["tenant_id"] = "tenant-b"
    with pytest.raises(ValueError):
        api().validate_note(note, evidence, score)


def test_note_context_preserves_raw_effective_calibration_and_confidence_separately():
    _, evidence, score = sample()
    config = config_fixture()
    config.update(score_mode="calibrated", calibration_id="b" * 64)
    identified(config, "config_id")
    decision = decision_fixture(config=config, evidence=evidence)
    decision["effective_probability"] = "0.7"
    identified(decision, "decision_id")
    original = json.dumps(score, sort_keys=True)
    request = api().build_note_request(decision, evidence, score, config)
    data = json.loads(request["messages"][0]["content"])
    assert data["routing"] == {
        "raw_probability": "0.2",
        "effective_probability": "0.7",
        "score_mode": "calibrated",
        "calibration_id": "b" * 64,
    }
    assert data["confidence"]["value"] == "0.8"
    assert "fraud_probability" not in data
    assert json.dumps(score, sort_keys=True) == original
    assert score["adjusted_probability"] is None and score["calibration_id"] is None

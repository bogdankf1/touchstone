"""One logical note, at most one separately accounted schema repair."""

import json
from datetime import UTC, datetime

from reckoner.contracts import content_id
from reckoner.v1.notes.prompt import MODEL, build_note_request
from reckoner.v1.notes.validate import CONTENT_FIELDS, confidence, validate_note


def parse_json(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("nonfinite JSON")

    return json.loads(content, object_pairs_hook=pairs, parse_constant=invalid)


def generate_note(
    case: dict,
    evidence: dict,
    score: dict,
    client: object,
    config: dict,
    *,
    protocol: dict | None = None,
) -> dict:
    if protocol is None or client is None:
        return {"status": "pending", "note": None, "attempts": []}
    if "approved" in protocol:
        # Consent is an external SHA-bound approval record, never a protocol flag.
        raise ValueError("protocol flags cannot establish consent")
    request = build_note_request(case, evidence, score, config)
    attempts = []
    # The same exact approved request is sent for repair. No provider retry layer.
    for stage in ("note-generation", "note-repair")[: min(2, protocol["maximum_attempts"])]:
        attempt = client.execute(request, stage=stage, protocol=protocol)
        attempts.append(attempt)
        if attempt["status"] != "responded":
            return {"status": attempt["status"], "note": None, "attempts": attempts}
        body = attempt["body"]
        if body.get("finish_reason") != "stop" or body.get("reported_model") not in {
            MODEL,
            MODEL.removeprefix("anthropic/"),
        }:
            return {"status": "failed", "note": None, "attempts": attempts}
        try:
            fields = parse_json(body.get("content"))
            if not isinstance(fields, dict) or set(fields) != set(CONTENT_FIELDS):
                raise ValueError("exactly six content fields required")
            timestamp = attempt.get("completed_at", datetime.now(UTC).isoformat())
            note = {
                **fields,
                "schema_version": "reckoner-case-note-v1",
                "tenant_id": case["tenant_id"],
                "case_id": case["decision_id"],
                "decision_id": case["decision_id"],
                "prompt_version": "note-v1",
                "requested_model": MODEL,
                "reported_model": MODEL,
                "generation_status": "succeeded"
                if confidence(score)["value"] is not None
                else "degraded",
                "evidence_id": evidence["evidence_id"],
                "call_id": attempt["call_id"],
                "started_at": attempts[0].get("started_at", timestamp),
                "completed_at": timestamp,
            }
            note["note_id"] = content_id(note)
            validate_note(note, evidence, score)
        except (ValueError, TypeError, KeyError):
            continue
        return {"status": "succeeded", "note": note, "attempts": attempts}
    return {"status": "invalid", "note": None, "attempts": attempts}

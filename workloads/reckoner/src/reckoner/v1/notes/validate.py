"""Strict six-field notes, with deterministic provenance and factual checks."""

import json
from copy import deepcopy

from reckoner.v1 import projection
from reckoner.v1.contracts import validate_v1

CONTENT_FIELDS = (
    "verdict_recommendation",
    "confidence",
    "risk_indicators",
    "entity_neighbourhood",
    "comparable_cases",
    "what_would_change_verdict",
)


def confidence(score):
    value = score.get("confidence") if score.get("attempt_status") == "responded" else None
    return {
        "value": value,
        "meaning": "jev_distribution_concentration" if value is not None else "unavailable",
    }


def neighbourhood(evidence):
    display = evidence.get("neighbourhood")
    summary = (
        "Neighbourhood unavailable."
        if display is None
        else json.dumps(
            {
                key: display[key]
                for key in (
                    "window_days",
                    "total_nodes",
                    "total_edges",
                    "total_transactions",
                    "truncated",
                    "cross_tenant",
                )
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return {"summary": summary, "evidence_refs": [evidence["evidence_id"]]}


def comparable(case, evidence_id):
    return {
        "transaction_id": case["transaction_id"],
        "summary": json.dumps(
            {k: v for k, v in case.items() if k not in {"transaction_id", "evidence_refs"}},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "evidence_refs": [evidence_id],
    }


def note_data(evidence, score):
    """Explicit inputs; source evidence has already passed the canonical contract."""
    return {
        "evidence_id": evidence["evidence_id"],
        "coverage": deepcopy(evidence["coverage"]),
        "confidence": confidence(score),
        "request_projection": projection.VERSION,
        # Bounded for the provider; validate_note checks claims against the full evidence.
        "risk_indicators": projection.indicators(evidence),
        "entity_neighbourhood": neighbourhood(evidence),
        "comparable_cases": [
            comparable(c, evidence["evidence_id"]) for c in projection.comparables(evidence)
        ],
    }


def validate_note(note: dict, evidence: dict, score: dict) -> dict:
    note = validate_v1("case-note", note)
    validate_v1("evidence", evidence)
    if score:
        validate_v1("score", score)
        if score["tenant_id"] != evidence["tenant_id"]:
            raise ValueError("score tenant does not match evidence")
    if note["generation_status"] not in {"succeeded", "degraded"}:
        raise ValueError("failed generation cannot be a valid note")
    if note["tenant_id"] != evidence["tenant_id"] or note["evidence_id"] != evidence["evidence_id"]:
        raise ValueError("note evidence identity mismatch")
    if note["confidence"] != confidence(score):
        raise ValueError("confidence must preserve Jev concentration, never fraud probability")
    if note["entity_neighbourhood"] != neighbourhood(evidence):
        raise ValueError("unsupported neighbourhood claim")
    allowed = note_data(evidence, score)
    persisted = {i["indicator_id"]: i for i in evidence["risk_indicators"]}
    supplied = {i["indicator_id"]: i for i in allowed["risk_indicators"]}
    for item in note["risk_indicators"]:
        source = persisted.get(item["indicator_id"])
        if source is None or any(item[k] != source[k] for k in ("description", "method")):
            raise ValueError("unsupported indicator or method")
        # References must be the full persisted list or exactly the exemplars supplied
        # to the provider, and always a subset of the full persisted references.
        refs = item["evidence_refs"]
        if not set(refs) <= set(source["evidence_refs"]) or refs not in (
            source["evidence_refs"],
            supplied[item["indicator_id"]]["evidence_refs"],
        ):
            raise ValueError("unsupported indicator references")
    if len({i["indicator_id"] for i in note["risk_indicators"]}) != len(note["risk_indicators"]):
        raise ValueError("duplicate indicator")
    if any(c not in allowed["comparable_cases"] for c in note["comparable_cases"]):
        raise ValueError("unsupported comparable claim")
    if len({c["transaction_id"] for c in note["comparable_cases"]}) != len(
        note["comparable_cases"]
    ):
        raise ValueError("duplicate comparable")
    refs = {evidence["evidence_id"]} | {
        r for i in evidence["risk_indicators"] for r in i["evidence_refs"]
    }
    for action in note["what_would_change_verdict"]:
        if not set(action["evidence_refs"]) <= refs:
            raise ValueError("unsupported evidence reference")
    return note

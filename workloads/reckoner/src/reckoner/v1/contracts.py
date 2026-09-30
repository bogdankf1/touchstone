"""Strict JSON records and content identities for the simulated v1 workflow."""

import json
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, localcontext

from jsonschema import ValidationError

from reckoner.contracts import content_id, validate_document
from reckoner.resources import SCHEMAS

KINDS = {
    "run-config",
    "evidence",
    "score",
    "decision",
    "case-note",
    "review",
    "resolution",
    "experiment",
}
IDENTITIES = {
    "run-config": "config_id",
    "evidence": "evidence_id",
    "decision": "decision_id",
    "case-note": "note_id",
    "resolution": "resolution_id",
    "experiment": "experiment_id",
}


def _identity(document: dict, key: str) -> None:
    if document[key] != content_id(
        {name: value for name, value in document.items() if name != key}
    ):
        raise ValueError(f"{key} does not match contents")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def validate_v1(kind: str, document: dict) -> dict:
    """Validate schema and cross-field semantics; return a detached JSON document."""
    if kind not in KINDS:
        raise ValueError("unsupported v1 record kind")
    try:
        json.dumps(document, allow_nan=False)
        validate_document(document, SCHEMAS / f"reckoner-{kind}-v1.schema.json")
    except (ValidationError, TypeError) as exc:
        raise ValueError("invalid v1 record") from exc
    if key := IDENTITIES.get(kind):
        _identity(document, key)
    if kind == "run-config":
        for name in ("scorer", "note_model", "judge_model"):
            model = document[name]
            price = model["price_table"]
            _identity(price, "price_table_version")
            if price["model"] != model["model"]:
                raise ValueError("model and price table do not match")
        if document["score_mode"] == "calibrated" and document["calibration_id"] is None:
            raise ValueError("calibrated mode requires calibration identity")
    elif kind == "score":
        distribution = document["distribution"]
        if document["attempt_status"] == "responded":
            if any(
                document[key] is None
                for key in ("distribution", "raw_probability", "confidence", "reported_model")
            ):
                raise ValueError("successful score requires a complete response")
        elif any(
            document[key] is not None
            for key in ("distribution", "raw_probability", "confidence", "adjusted_probability")
        ):
            raise ValueError("failed score cannot report probabilities")
        if distribution is not None:
            with localcontext() as context:
                context.prec = max(len(value) for value in distribution.values()) + 10
                if abs(
                    Decimal(distribution["fraud"]) + Decimal(distribution["legitimate"]) - 1
                ) > Decimal("0.000001"):
                    raise ValueError("binary probabilities must sum to one")
            if Decimal(document["raw_probability"]) != Decimal(distribution["fraud"]):
                raise ValueError("raw probability must match fraud choice")
        if (document["adjusted_probability"] is None) != (document["calibration_id"] is None):
            raise ValueError("adjusted probability requires calibration identity")
        cost = document["cost"]
        if cost["status"] == "settled":
            if cost["amount"] is None or any(value is None for value in document["usage"].values()):
                raise ValueError("settled cost requires known usage and amount")
        elif cost["amount"] is not None:
            raise ValueError("unknown cost cannot report a settled amount")
    elif kind == "decision":
        if document["scorer_status"] == "succeeded":
            if any(
                document[key] is None
                for key in ("call_id", "raw_probability", "effective_probability")
            ):
                raise ValueError("successful decision requires scored call")
            if document["degraded_reason"] is not None:
                raise ValueError("successful decision cannot be degraded")
            probability = Decimal(document["effective_probability"])
            low = Decimal(document["effective_low_threshold"])
            high = Decimal(document["effective_high_threshold"])
            outcome = (
                "auto-approve"
                if probability < low
                else ("auto-decline" if probability > high else "escalate")
            )
            if document["outcome"] != outcome:
                raise ValueError("decision outcome does not match effective thresholds")
        elif (
            document["outcome"] != "escalate"
            or document["degraded_reason"] is None
            or document["raw_probability"] is not None
            or document["effective_probability"] is not None
        ):
            raise ValueError("degraded decision must escalate without a fabricated score")
        if Decimal(document["effective_low_threshold"]) >= Decimal(
            document["effective_high_threshold"]
        ):
            raise ValueError("invalid effective thresholds")
    elif kind == "evidence":
        query_time = _time(document["query_time"])
        for cutoff in document["cutoffs"].values():
            if cutoff is not None and _time(cutoff) > query_time:
                raise ValueError("evidence cutoff cannot follow query time")
        if projection := document.get("graph_projection"):
            projection_body = {
                k: v
                for k, v in projection.items()
                if k not in {"projection_id", "snapshot_age_seconds"}
            }
            if projection["projection_id"] != content_id(projection_body):
                raise ValueError("graph projection identity mismatch")
            if _time(projection["cutoff"]) > query_time:
                raise ValueError("graph projection cannot follow query time")
            if (
                document["cutoffs"]["graph_before"] != projection["cutoff"]
                or document["source_snapshot_ids"]["graph"] != projection["projection_id"]
            ):
                raise ValueError("graph projection provenance mismatch")
            if Decimal(projection["snapshot_age_seconds"]) != Decimal(
                str((query_time - _time(projection["cutoff"])).total_seconds())
            ):
                raise ValueError("graph snapshot age mismatch")
        if display := document.get("neighbourhood"):
            nodes = {n["id"] for n in display["nodes"]}
            if len(nodes) != len(display["nodes"]) or len(nodes) > display["total_nodes"]:
                raise ValueError("invalid neighbourhood node totals")
            if len(display["edges"]) > display["total_edges"] or any(
                e["source"] not in nodes or e["target"] not in nodes for e in display["edges"]
            ):
                raise ValueError("invalid neighbourhood edges")
            if len(display["transaction_refs"]) != display["total_transactions"]:
                raise ValueError("invalid neighbourhood transaction totals")
            if display["truncated"] != (
                len(nodes) < display["total_nodes"]
                or len(display["edges"]) < display["total_edges"]
            ):
                raise ValueError("invalid neighbourhood truncation flag")
        for case in document["comparable_cases"]:
            if _time(case["resolved_at"]) >= _time(document["cutoffs"]["resolved_before"]):
                raise ValueError("comparable resolution must precede cutoff strictly")
        if document["coverage"]["status"] == "available" and document["coverage"]["missing"]:
            raise ValueError("available evidence cannot have missing coverage")
    elif kind == "case-note":
        confidence = document["confidence"]
        if (confidence["value"] is None) != (confidence["meaning"] == "unavailable"):
            raise ValueError("unavailable confidence must have no value")
        if document["generation_status"] == "succeeded" and (
            document["call_id"] is None or document["reported_model"] is None
        ):
            raise ValueError("generated note requires a successful call")
    elif kind == "review":
        references = (document["oracle_version"], document["resolution_policy_version"])
        if document["reviewer_type"] == "simulated" and any(value is None for value in references):
            raise ValueError("simulated review requires oracle and resolution policy")
        if document["reviewer_type"] == "human" and any(value is not None for value in references):
            raise ValueError("human review cannot carry simulated oracle provenance")
    elif kind == "experiment":
        tasks = document["tasks"]
        if len({task["task_id"] for task in tasks}) != len(tasks) or len(
            {task["transaction_id"] for task in tasks}
        ) != len(tasks):
            raise ValueError("experiment task and transaction identities must be unique")
    if kind in {"decision", "case-note"}:
        if _time(document["completed_at"]) < _time(document["started_at"]):
            raise ValueError("completion cannot precede start")
    if kind in {"evidence", "case-note"}:
        ranks = [indicator["rank"] for indicator in document["risk_indicators"]]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("risk indicators must be ranked consecutively")
    return deepcopy(document)

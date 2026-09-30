"""Pure Decimal routing and graph steps backed by immutable documents."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from langgraph.runtime import Runtime

from reckoner.contracts import content_id
from reckoner.v1.calibration import apply_calibration, validate_calibration_context
from reckoner.v1.storage.attempts import score_task
from reckoner.v1.workflow.runner import IDENTITY, calibration_context, configuration


def route(probability: Decimal | None, amount: Decimal, thresholds: dict) -> dict:
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be positive finite USD")
    try:
        floor, ceiling, high, cost = (
            Decimal(thresholds[key])
            for key in ("t_low_floor", "t_low_ceiling", "t_high", "review_cost")
        )
        if not all(value.is_finite() for value in (floor, ceiling, high, cost)):
            raise ValueError("finite thresholds required")
        if not 0 <= floor <= ceiling < high <= 1 or cost < 0:
            raise ValueError("invalid threshold order or review cost")
        if thresholds["currency"] != "USD" or type(thresholds["amount_aware"]) is not bool:
            raise ValueError("USD and explicit amount-aware switch required")
        if not thresholds["amount_aware"] and high <= Decimal(".05"):
            raise ValueError("high threshold must exceed flat low")
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise ValueError("invalid thresholds") from exc
    low = max(floor, min(ceiling, cost / amount)) if thresholds["amount_aware"] else Decimal(".05")
    if probability is not None and (
        not isinstance(probability, Decimal)
        or not probability.is_finite()
        or not 0 <= probability <= 1
    ):
        raise ValueError("invalid probability")
    return {
        "outcome": _outcome(probability, low, high),
        "effective_low_threshold": format(low, "f"),
        "effective_high_threshold": format(high, "f"),
    }


def _outcome(probability, low, high):
    if probability is not None:
        if probability < low:
            return "auto-approve"
        if probability > high:
            return "auto-decline"
    return "escalate"


def _identity(state):
    return {key: state[key] for key in IDENTITY}


def intake(state, runtime: Runtime):
    repo = runtime.context["repo"]
    task = repo.task(**_identity(state))
    config = configuration(repo, task)
    threshold = repo.workflow_document(
        "threshold_configs",
        {"tenant_id": state["tenant_id"], "config_id": config["threshold_config_id"]},
    )
    if task["transaction"]["currency"] != "USD":
        raise ValueError("workflow requires USD transaction")
    routing = route(
        None,
        Decimal(task["transaction"]["amount_minor"]) / 100,
        {**threshold["parameters"], "currency": threshold["currency"]},
    )
    return {key: value for key, value in routing.items() if key != "outcome"}


def evidence(state, runtime: Runtime):
    document = runtime.context["repo"].workflow_document(
        "v1_evidence", {"tenant_id": state["tenant_id"], "evidence_id": state["evidence_id"]}
    )
    projection = document.get("graph_projection")
    rank = (
        ("converged" if projection["page_rank_converged"] else "nonconverged")
        if projection
        else "unavailable"
    )
    status = document["coverage"]["status"]
    policy = runtime.context["repo"].workflow_document(
        "v1_workflow_runs", {"tenant_id": state["tenant_id"], "run_id": state["run_id"]}
    )
    if policy["evidence_mode"] == "gds-augmented" and projection is None:
        status = "unavailable"
    return {
        "evidence_status": status,
        "pagerank_status": rank,
        "degraded_reason": "evidence_unavailable" if status != "available" else None,
    }


def score(state, runtime: Runtime):
    if state["evidence_status"] != "available":
        return {
            "scorer_status": "unavailable",
            "raw_probability": None,
            "call_id": None,
            "cost_status": "not_dispatched",
        }
    repo = runtime.context["repo"]
    binding = repo.workflow_document("v1_workflow_tasks", _identity(state))
    document = repo.workflow_document(
        "v1_evidence", {"tenant_id": state["tenant_id"], "evidence_id": state["evidence_id"]}
    )
    if repo.scorer_client is None:
        raise ValueError("explicit scorer client required for scoring/resume")
    result = score_task(
        repo, repo.scorer_client, repo.task(**_identity(state)), document, binding["protocol"]
    )
    if result.get("scorer_status") == "unavailable":
        return {
            "scorer_status": "unavailable",
            "raw_probability": None,
            "call_id": None,
            "cost_status": "not_dispatched",
            "degraded_reason": "scorer_unavailable",
        }
    status = "succeeded" if result["attempt_status"] == "responded" else result["attempt_status"]
    return {
        "scorer_status": status,
        "raw_probability": result["raw_probability"],
        "call_id": result["call_id"],
        "cost_status": result["cost"]["status"],
        "degraded_reason": None if status == "succeeded" else "scorer_" + status,
    }


def calibrate_route(state, runtime: Runtime):
    repo = runtime.context["repo"]
    config = configuration(repo, state)
    policy = repo.workflow_document(
        "v1_workflow_runs", {"tenant_id": state["tenant_id"], "run_id": state["run_id"]}
    )
    effective = None
    if state["raw_probability"] is not None:
        artifact = policy["calibration"]
        if config["score_mode"] == "calibrated":
            if artifact is None or artifact["calibration_id"] != config["calibration_id"]:
                raise ValueError("calibration identity mismatch")
            validate_calibration_context(artifact, calibration_context(config, policy))
        effective = apply_calibration(Decimal(state["raw_probability"]), artifact)
    low, high = (
        Decimal(state["effective_low_threshold"]),
        Decimal(state["effective_high_threshold"]),
    )
    return {
        "effective_probability": format(effective, "f") if effective is not None else None,
        "outcome": _outcome(effective, low, high),
    }


def persist_decision(state, runtime: Runtime):
    repo = runtime.context["repo"]
    existing = repo.workflow_document("v1_decisions", _identity(state))
    if existing is None:
        document = {
            key: state[key]
            for key in (
                *IDENTITY,
                "transaction_id",
                "config_id",
                "evidence_id",
                "call_id",
                "raw_probability",
                "effective_probability",
                "effective_low_threshold",
                "effective_high_threshold",
                "outcome",
                "scorer_status",
                "degraded_reason",
                "started_at",
            )
        }
        document.update(
            schema_version="reckoner-decision-v1", completed_at=datetime.now(UTC).isoformat()
        )
        document["decision_id"] = content_id(document)
        existing = repo.persist_decision(document)
    return {"decision_id": existing["decision_id"], "status": "completed"}

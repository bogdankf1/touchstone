"""Deterministic oracle evaluation for the frozen simulated baseline."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

EVALUATOR_VERSION = "reckoner-cost-v1"
OUTCOME_VERSION = "reckoner-outcome-v1"


def _decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def evaluate_case(
    outcome: str | None,
    label: str,
    amount_minor: int,
    review_cost: Decimal,
    margin_rate: Decimal,
    missing_status: str = "failed",
) -> dict[str, Any]:
    """Evaluate one decision with exact decimal business costs."""
    if label not in {"legitimate", "fraud"}:
        raise ValueError("invalid oracle label")
    if outcome is None:
        if missing_status not in {"failed", "pending"}:
            raise ValueError("invalid missing outcome status")
        return {
            "status": missing_status,
            "correct": None,
            "review_cost": None,
            "error_cost": None,
            "currency": None,
            "outcome_version": OUTCOME_VERSION,
            "evaluator_version": EVALUATOR_VERSION,
        }
    if outcome not in {"auto-approve", "auto-decline", "escalate"}:
        raise ValueError("invalid decision outcome")

    amount = Decimal(amount_minor) / Decimal(100)
    correct = (
        outcome == "escalate"
        or (outcome == "auto-approve" and label == "legitimate")
        or (outcome == "auto-decline" and label == "fraud")
    )
    review = review_cost if outcome == "escalate" else Decimal(0)
    if outcome == "auto-approve" and label == "fraud":
        error = amount
    elif outcome == "auto-decline" and label == "legitimate":
        error = amount * margin_rate
    else:
        error = Decimal(0)
    return {
        "status": "observed",
        "correct": correct,
        "review_cost": _decimal(review),
        "error_cost": _decimal(error),
        "currency": "USD",
        "outcome_version": OUTCOME_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
    }


def evaluate_run(repo, run_id: str) -> dict[str, Any]:
    """Persist one idempotent evaluation and generic telemetry record per fixed task."""
    rows = repo.evaluation_inputs(run_id)
    errors = 0
    for row in rows:
        result = evaluate_case(
            row["outcome"],
            row["label"],
            row["amount_minor"],
            row["review_cost"],
            row["margin_rate"],
            "pending" if row["task_status"] in {"pending", "dispatched"} else "failed",
        )
        result["schema_valid"] = row["outcome"] is not None
        if result["status"] != "observed":
            result["required_suite_status"] = "error"
            result["evaluation_errors"] = ["missing_outcome"]
            errors += 1
        else:
            result["required_suite_status"] = "pass" if result["correct"] else "fail"
            result["evaluation_errors"] = []
        repo.persist_evaluation(row, result)
    return {"run_id": run_id, "evaluated": len(rows), "errors": errors}

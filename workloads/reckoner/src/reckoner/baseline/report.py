"""Pure, deterministic baseline report aggregation and rendering."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any


def _decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _rate(numerator: int, denominator: int) -> dict[str, int | str | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": _decimal(Decimal(numerator) / Decimal(denominator)) if denominator else None,
    }


def _nearest_rank_p99(values: list[Decimal]) -> str | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (99 * len(ordered) + 99) // 100
    return _decimal(ordered[rank - 1])


def _metrics(snapshot: dict[str, Any], tenant_id: str | None) -> dict[str, Any]:
    tasks = [
        task for task in snapshot["tasks"] if tenant_id is None or task["tenant_id"] == tenant_id
    ]
    task_ids = {(task["tenant_id"], task["task_id"]) for task in tasks}
    evaluations = [
        item for item in snapshot["evaluations"] if (item["tenant_id"], item["task_id"]) in task_ids
    ]
    attempts = [
        item for item in snapshot["attempts"] if (item["tenant_id"], item["task_id"]) in task_ids
    ]
    expected = len(tasks)
    completed = sum(task["status"] == "completed" for task in tasks)
    correct = sum(item["document"]["correct"] is True for item in evaluations)
    legitimate = sum(task.get("label_class") == "legitimate" for task in tasks)
    fraud = sum(task.get("label_class") == "fraud" for task in tasks)
    false_positives = sum(
        item["outcome"] == "auto-decline" and item["label_class"] == "legitimate"
        for item in evaluations
    )
    missed_fraud = sum(
        item["outcome"] == "auto-approve" and item["label_class"] == "fraud" for item in evaluations
    )
    escalations = sum(item["outcome"] == "escalate" for item in evaluations)
    schema_valid = sum(item["document"].get("schema_valid") is True for item in evaluations)
    suite_passes = sum(
        item["document"].get("required_suite_status") == "pass" for item in evaluations
    )
    suite_errors = sum(
        item["document"].get("required_suite_status") == "error" for item in evaluations
    )

    by_call: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        by_call.setdefault(attempt["call_id"], attempt)
    known_model_costs = [
        Decimal(attempt["actual_cost"])
        for attempt in by_call.values()
        if attempt["actual_cost"] is not None
    ]
    unknown_costs = sum(attempt["actual_cost"] is None for attempt in by_call.values())
    reserved_unsettled = sum(
        (
            Decimal(attempt["maximum_cost"])
            if attempt["actual_cost"] is None and attempt["status"] in {"dispatched", "uncertain"}
            else Decimal(0)
        )
        for attempt in by_call.values()
    )
    review = sum(
        (
            Decimal(item["document"]["review_cost"])
            if item["document"]["review_cost"] is not None
            else Decimal(0)
        )
        for item in evaluations
    )
    error = sum(
        (
            Decimal(item["document"]["error_cost"])
            if item["document"]["error_cost"] is not None
            else Decimal(0)
        )
        for item in evaluations
    )
    model = sum(known_model_costs, Decimal(0))
    total = model + review + error
    incomplete_outcomes = (
        any(item["document"]["status"] != "observed" for item in evaluations)
        or len(evaluations) != expected
    )
    if unknown_costs:
        cpst_availability = "unknown_provider_cost"
    elif incomplete_outcomes or completed != expected:
        cpst_availability = "incomplete_outcomes"
    elif correct == 0:
        cpst_availability = "no_correct_outcomes"
    else:
        cpst_availability = "available"
    cpst_available = cpst_availability == "available"

    completed_ids = {
        (task["tenant_id"], task["task_id"]) for task in tasks if task["status"] == "completed"
    }
    durations = []
    seen_tasks = set()
    for attempt in attempts:
        task_key = (attempt["tenant_id"], attempt["task_id"])
        if (
            task_key in completed_ids
            and task_key not in seen_tasks
            and attempt.get("duration_ms") is not None
        ):
            durations.append(Decimal(attempt["duration_ms"]))
            seen_tasks.add(task_key)

    suite_rate = _rate(suite_passes, expected)
    suite_rate["status"] = (
        "error"
        if suite_errors or len(evaluations) != expected
        else "pass"
        if suite_passes == expected
        else "fail"
    )
    return {
        "correctness": _rate(correct, expected),
        "false_positive_rate": _rate(false_positives, legitimate),
        "missed_fraud_rate": _rate(missed_fraud, fraud),
        "escalation_rate": _rate(escalations, expected),
        "completion_rate": _rate(completed, expected),
        "response_schema_validity": _rate(schema_valid, expected),
        "required_suite_pass_rate": suite_rate,
        "costs": {
            "model": _decimal(model),
            "review": _decimal(review),
            "error": _decimal(error),
            "total": _decimal(total),
            "currency": "USD",
            "unknown_provider_costs": unknown_costs,
            "reserved_unsettled": _decimal(reserved_unsettled),
        },
        "cpst": {
            "numerator": _decimal(total) if cpst_available else None,
            "denominator": correct,
            "value": _decimal(total / Decimal(correct)) if cpst_available else None,
            "currency": "USD",
            "availability": cpst_availability,
        },
        "latency_ms": {
            "population": len(durations),
            "p99_nearest_rank": _nearest_rank_p99(durations),
        },
    }


def build_report(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Build the evidence document without reading clocks, files, or external state."""
    tasks = snapshot["tasks"]
    counts = {
        status: sum(task["status"] == status for task in tasks)
        for status in ("completed", "failed", "uncertain", "pending", "dispatched")
    }
    counts["expected"] = len(tasks)
    evaluated_keys = {(item["tenant_id"], item["task_id"]) for item in snapshot["evaluations"]}
    missing_evaluations = [
        task for task in tasks if (task["tenant_id"], task["task_id"]) not in evaluated_keys
    ]
    counts["evaluation_errors"] = len(missing_evaluations) + sum(
        bool(item["document"].get("evaluation_errors")) for item in snapshot["evaluations"]
    )
    aggregate_metrics = _metrics(snapshot, None)
    per_tenant = {
        tenant_id: {"metrics": _metrics(snapshot, tenant_id)}
        for tenant_id in sorted(snapshot["tenants"])
    }
    run = dict(snapshot["run"])
    if aggregate_metrics["cpst"]["availability"] in {
        "unknown_provider_cost",
        "incomplete_outcomes",
    }:
        run["status"] = "incomplete"
    enriched_mix = (
        "2 fraud and 18 legitimate" if run["purpose"] == "pilot" else "100 fraud and 900 legitimate"
    )
    return {
        "schema_version": "reckoner-report-v1",
        "report_version": "1",
        "run": run,
        "counts": counts,
        "aggregate": {"metrics": aggregate_metrics},
        "per_tenant": per_tenant,
        "evaluation_errors": [
            {
                "tenant_id": item["tenant_id"],
                "task_id": item["task_id"],
                "errors": item["document"]["evaluation_errors"],
            }
            for item in snapshot["evaluations"]
            if item["document"].get("evaluation_errors")
        ]
        + [
            {
                "tenant_id": task["tenant_id"],
                "task_id": task["task_id"],
                "errors": ["evaluation_missing"],
            }
            for task in missing_evaluations
        ],
        "exports": snapshot.get("exports", []),
        "caveats": [
            "All transaction data is simulated; no production or real-customer claim is made.",
            (
                f"The evaluation cohort is enriched to {enriched_mix} purchases and does not "
                "represent population prevalence."
            ),
            (
                "USD amounts and UTC timestamps are experimental assumptions about the "
                "source generator."
            ),
            (
                "Escalation correctness assumes an ideal reviewer; no human-agreement "
                "result is available."
            ),
            (
                "Case-note quality, faithfulness, calibration, and human agreement are not "
                "evaluated in this phase."
            ),
        ],
    }


def _available(value: Any) -> str:
    return "Unavailable" if value is None else str(value)


def _markdown(report: dict[str, Any]) -> str:
    metrics = report["aggregate"]["metrics"]
    cpst = metrics["cpst"]
    lines = [
        f"# Reckoner baseline report: {report['run']['run_id']}",
        "",
        f"Run status: **{report['run']['status']}**",
        "",
        "## Aggregate metrics",
        "",
        f"- CPST: {_available(cpst['value'])} USD ({cpst['availability']})",
        (
            f"- Correct decisions: {metrics['correctness']['numerator']}/"
            f"{metrics['correctness']['denominator']}"
        ),
        (
            f"- Completed tasks: {metrics['completion_rate']['numerator']}/"
            f"{metrics['completion_rate']['denominator']}"
        ),
        f"- Model cost: {metrics['costs']['model']} USD",
        f"- Review cost: {metrics['costs']['review']} USD",
        f"- Error cost: {metrics['costs']['error']} USD",
        f"- p99 completed-task latency: {_available(metrics['latency_ms']['p99_nearest_rank'])} ms",
        "",
        "## Caveats",
        "",
        *(f"- {caveat}" for caveat in report["caveats"]),
        "",
    ]
    return "\n".join(lines)


def write_report(report: dict[str, Any], output: Path) -> None:
    """Write stable JSON and Markdown renderings of the same evidence document."""
    output.mkdir(parents=True, exist_ok=True)
    json_body = json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    (output / "report.json").write_text(json_body, encoding="utf-8")
    (output / "report.md").write_text(_markdown(report), encoding="utf-8")

"""Pure, deterministic baseline report aggregation and rendering."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from reckoner.contracts import validate_document
from reckoner.resources import SCHEMAS

REPORT_SCHEMA = SCHEMAS / "reckoner-report-v1.schema.json"


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
    provider_call_mode = run["provider_call_mode"]
    if provider_call_mode == "fake":
        enriched_mix = f"{len(tasks)} fabricated fixture"
    if provider_call_mode == "fake":
        provider_caveat = (
            "Provider calls came from the explicit fake provider; they are test evidence and "
            "not measured external model calls."
        )
    elif provider_call_mode == "measured":
        provider_caveat = (
            "External provider calls were measured from provider-reported usage; calculated "
            "cost is not invoice reconciliation."
        )
    else:
        provider_caveat = "Provider call mode is unavailable; model-call provenance is incomplete."
    report = {
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
            provider_caveat,
        ],
    }
    _validate_report(report)
    return report


def _available(value: Any) -> str:
    return "Unavailable" if value is None else str(value)


def _validate_report(report: dict[str, Any]) -> None:
    try:
        validate_document(report, REPORT_SCHEMA)
    except (ValidationError, OSError, TypeError, ValueError) as error:
        raise ValueError("invalid report document") from error


def _rate_line(label: str, rate: dict[str, Any]) -> str:
    return (
        f"- {label}: {rate['numerator']}/{rate['denominator']} (value: {_available(rate['value'])})"
    )


def _metric_lines(metrics: dict[str, Any]) -> list[str]:
    costs = metrics["costs"]
    cpst = metrics["cpst"]
    latency = metrics["latency_ms"]
    return [
        f"- CPST: {_available(cpst['value'])} USD ({cpst['availability']})",
        (
            f"- CPST numerator / correct-decision denominator: "
            f"{_available(cpst['numerator'])}/{cpst['denominator']}"
        ),
        _rate_line("Correctness", metrics["correctness"]),
        _rate_line("False-positive rate", metrics["false_positive_rate"]),
        _rate_line("Missed-fraud rate", metrics["missed_fraud_rate"]),
        _rate_line("Escalation rate", metrics["escalation_rate"]),
        _rate_line("Completion rate", metrics["completion_rate"]),
        _rate_line("Response-schema validity", metrics["response_schema_validity"]),
        (
            _rate_line("Required-suite pass rate", metrics["required_suite_pass_rate"])
            + f"; status: {metrics['required_suite_pass_rate']['status']}"
        ),
        f"- Model cost: {costs['model']} USD",
        f"- Review cost: {costs['review']} USD",
        f"- Error cost: {costs['error']} USD",
        f"- Total observed cost: {costs['total']} USD",
        f"- Unknown provider costs: {costs['unknown_provider_costs']}",
        f"- Reserved unsettled: {costs['reserved_unsettled']} USD",
        f"- Completed-task latency population: {latency['population']}",
        f"- p99 completed-task latency: {_available(latency['p99_nearest_rank'])} ms",
    ]


def _markdown(report: dict[str, Any]) -> str:
    metrics = report["aggregate"]["metrics"]
    run = report["run"]
    counts = report["counts"]
    lines = [
        f"# Reckoner baseline report: {run['run_id']}",
        "",
        f"Run status: **{run['status']}**",
        "",
        "## Run provenance",
        "",
        f"- Purpose: {run['purpose']}",
        f"- Execution mode: {run['execution_mode']}",
        f"- Provider call mode: {_available(run['provider_call_mode'])}",
        f"- Config ID: {run['config_id']}",
        f"- Bundle ID: {run['bundle_id']}",
        f"- Prompt version: {run['prompt_version']}",
        f"- Model: {run['model']}",
        f"- Price table version: {run['price_table_version']}",
        f"- Threshold config IDs: {json.dumps(run['threshold_config_ids'], sort_keys=True)}",
        f"- Cohort IDs: {json.dumps(run['cohort_ids'], sort_keys=True)}",
        f"- Code revision: {_available(run['code_revision'])}",
        f"- Created at: {run['created_at']}",
        f"- Preflight at: {_available(run['preflight_at'])}",
        "",
        "## Counts",
        "",
        f"- Expected: {counts['expected']}",
        f"- Completed: {counts['completed']}",
        f"- Failed: {counts['failed']}",
        f"- Uncertain: {counts['uncertain']}",
        f"- Pending: {counts['pending']}",
        f"- Dispatched: {counts['dispatched']}",
        f"- Evaluation errors: {counts['evaluation_errors']}",
        "",
        "## Aggregate metrics",
        "",
        *_metric_lines(metrics),
    ]
    for tenant_id, tenant in sorted(report["per_tenant"].items()):
        lines.extend(["", f"## Tenant {tenant_id}", "", *_metric_lines(tenant["metrics"])])
    lines.extend(["", "## Evaluation errors", ""])
    if report["evaluation_errors"]:
        lines.extend(
            f"- {item['tenant_id']} / {item['task_id']}: {', '.join(item['errors'])}"
            for item in report["evaluation_errors"]
        )
    else:
        lines.append("- None")
    lines.extend(["", "## Trace and export references", ""])
    if report["exports"]:
        lines.extend(
            (f"- {item['producer']} / {item['status']} / {item['event_id']} / {item['task_id']}")
            for item in report["exports"]
        )
    else:
        lines.append("- None")
    lines.extend(["", "## Caveats", "", *(f"- {caveat}" for caveat in report["caveats"]), ""])
    return "\n".join(lines)


def write_report(report: dict[str, Any], output: Path) -> None:
    """Write stable JSON and Markdown renderings of the same evidence document."""
    _validate_report(report)
    output.mkdir(parents=True, exist_ok=True)
    json_body = json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    (output / "report.json").write_text(json_body, encoding="utf-8")
    (output / "report.md").write_text(_markdown(report), encoding="utf-8")

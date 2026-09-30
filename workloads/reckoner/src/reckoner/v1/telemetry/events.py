"""Workload-owned mapping; no raw evidence, oracle labels or provider bodies leave here."""

from datetime import datetime
from decimal import Decimal

from reckoner.contracts import content_id, validate_document
from reckoner.resources import SCHEMAS

NOTE_SUITE = {
    "suite_id": "reckoner-note-v1",
    "suite_version": "v1",
    "expected_case_ids": [],
    "required_checks": ["schema", "agreement", "faithfulness"],
}
PURPOSE_SCOPES = {
    "final": "online",
    "online-note": "online",
    "fabricated": "online",
    "pilot": "offline",
    "calibration": "offline",
    "development": "offline",
    "validation": "offline",
    "graph-comparison": "offline",
    "judge": "offline",
}


def _tokens(value):
    return value if type(value) is int and value >= 0 else None


def cost_scope(purpose):
    if purpose not in PURPOSE_SCOPES:
        raise ValueError("unknown provider purpose")
    return PURPOSE_SCOPES[purpose]


def run_declaration(run):
    m = run["manifest"]
    tasks = [t["task_id"] for t in m["tasks"]]
    doc = {
        "schema_version": "run-declaration-v1",
        "event_id": content_id([m["tenant_id"], m["run_id"], "declaration"]),
        "tenant_id": m["tenant_id"],
        "workflow_id": "reckoner",
        "workflow_version": "reckoner-v1",
        "run_id": m["run_id"],
        "declared_at": m["created_at"],
        "expected_task_ids": tasks,
        "expected_task_count": len(tasks),
        "experiment_version": m["experiment_id"],
        **{k: m[k] for k in ("cohort_version", "code_revision", "dataset_version")},
        "config_version": m["config_id"],
        "measurement_mode": "fabricated" if m["purpose"] == "fabricated" else "measured",
        "dataset_simulated": True,
        "replay": {"is_replay": False, "source_manifest_sha256": None},
        "lifecycle_version": "root-work-v1",
        "evaluation_suites": [NOTE_SUITE],
        "metric_expectations": [
            {
                "metric_id": key,
                "definition_version": "reckoner-metrics-v1",
                "unit": "decision",
                "expected_task_ids": tasks,
            }
            for key in ("false_positive", "missed_fraud", "escalation")
        ],
    }
    if run.get("telemetry_mode") == "scoring-only":
        doc["evaluation_suites"] = []
        doc["metric_expectations"] = []
    if "config" in run:
        pinned = {}
        for key in ("scorer", "note_model", "judge_model"):
            model = run["config"][key]
            identity = (model["provider"], model["model"])
            price = model["price_table"]["price_table_version"]
            if identity in pinned and pinned[identity] != price:
                raise ValueError("conflicting pinned provider/model prices")
            pinned[identity] = price
        doc["provider_prices"] = [
            {"provider": provider, "model": model, "price_table_version": price}
            for (provider, model), price in sorted(pinned.items())
        ]
    validate_document(doc, SCHEMAS / "run-declaration-v1.schema.json")
    return doc


def _duration(start, end):
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000


def measurement_events(run: dict, task: dict, outcomes: list[dict]) -> list[dict]:
    """Map a persisted run {manifest,config} and task bundle; outcomes are evaluator-owned.

    task contains decision, normalized calls, optional note_result, reviews and evaluations.
    Work closures are produced separately under the database lifecycle lock.
    """
    if task.get("scoring"):
        return scoring_events(run, task)
    m, config, d = run["manifest"], run["config"], task["decision"]
    if (d["tenant_id"], d["run_id"], d["config_id"]) != (
        m["tenant_id"],
        m["run_id"],
        m["config_id"],
    ):
        raise ValueError("task/run identity mismatch")
    identity = [d[k] for k in ("tenant_id", "run_id", "task_id")]
    trace_id = content_id(["trace", *identity])[:32]
    reproduction = {
        "experiment_version": m["experiment_id"],
        "cohort_version": m["cohort_version"],
        "config_version": m["config_id"],
        "code_revision": m["code_revision"],
        "dataset_version": m["dataset_version"],
        "prompt_version": None,
        "model_version": None,
        "scorer_version": config["scorer"]["question_version"],
        "graph_version": config.get("graph_version"),
    }
    events = []

    def emit(kind, key, payload, at=None, node="decision", task_id=None, event_id=None):
        eid = event_id or content_id([*identity, kind, key])
        doc = {
            "schema_version": "measurement-v1",
            "event_id": eid,
            "tenant_id": d["tenant_id"],
            "workflow_id": "reckoner",
            "workflow_version": "reckoner-v1",
            "run_id": d["run_id"],
            "task_id": task_id or d["task_id"],
            "trace_id": trace_id,
            "span_id": content_id(["span", eid])[:16],
            "node_name": node,
            "event_kind": kind,
            "occurred_at": at or d["completed_at"],
            "simulated": m["purpose"] == "fabricated",
            "reproducibility": reproduction,
            "payload": payload,
        }
        validate_document(doc, SCHEMAS / "measurement-v1.schema.json")
        events.append(doc)

    start, end = d["started_at"], d["completed_at"]
    execution = {
        "started_at": start,
        "ended_at": end,
        "duration_ms": _duration(start, end),
        "status": "completed",
        "attempt_number": 1,
        "parent_task_id": None,
        "parent_span_id": None,
        "provider": None,
        "model": None,
    }
    emit("execution", "root", execution)
    note_task = content_id(["note-work", *identity])
    escalated = d["outcome"] == "escalate"
    emit(
        "work_declaration",
        "root",
        {
            "child_task_ids": [note_task] if escalated else [],
            "evaluation_suites": [NOTE_SUITE | {"expected_case_ids": [d["decision_id"]]}]
            if escalated
            else [],
        },
    )
    for call in task.get("calls", []):
        if call.get("pending"):
            continue
        scope = cost_scope(call["purpose"])
        amount = call["cost"]
        usage = call.get("usage") or {}
        emit(
            "provider_usage",
            call["call_id"],
            {
                "provider": call["provider"],
                "model": call["model"],
                "call_id": call["call_id"],
                "input_tokens": _tokens(usage.get("input_tokens")),
                "output_tokens": _tokens(usage.get("output_tokens")),
                "cached_tokens": _tokens(usage.get("cached_tokens")),
                "cost_scope": scope,
                "cost_status": "actual" if amount is not None else "unavailable",
                "cost_amount": format(Decimal(amount), "f") if amount is not None else None,
                "currency": "USD" if amount is not None else None,
                "price_table_version": call["price_table_version"],
            },
            at=call["occurred_at"],
            node="provider." + call["provider"],
        )
    note = task.get("note_result")
    if note is not None:
        times = [a["completed_at"] for a in note["attempts"]]
        ended = max(times)
        emit(
            "execution",
            "note",
            execution
            | {
                "ended_at": ended,
                "duration_ms": _duration(start, ended),
                "status": "completed" if note["status"] == "succeeded" else "failed",
                "parent_task_id": d["task_id"],
            },
            at=ended,
            node="note",
            task_id=note_task,
        )
    for result in outcomes:
        if any(result[k] != d[k] for k in ("tenant_id", "run_id", "task_id")):
            raise ValueError("outcome identity mismatch")
        emit(
            "outcome",
            result["outcome_version"],
            {
                k: result[k]
                for k in (
                    "status",
                    "correct",
                    "review_cost",
                    "error_cost",
                    "currency",
                    "outcome_version",
                    "evaluator_version",
                )
            },
            at=result["occurred_at"],
            node="evaluate",
        )
        for metric, values in result.get("rate_contributions", {}).items():
            emit(
                "metric_contribution",
                metric,
                {
                    "metric_id": metric,
                    "numerator": values["numerator"],
                    "denominator": values["denominator"],
                    "unit": "decision",
                    "definition_version": "reckoner-metrics-v1",
                    "cost_component_id": None,
                },
                at=result["occurred_at"],
                node="evaluate",
            )
    for review in task.get("reviews", []):
        if any(review[k] != d[k] for k in ("tenant_id", "run_id", "task_id")):
            raise ValueError("review identity mismatch")
        recommendation = review["recommendation"]
        emit(
            "review",
            review["action_id"],
            {
                "recommendation": recommendation,
                "review_id": review["action_id"],
                "reviewer_id": review["reviewer_type"],
                "reviewer_type": review["reviewer_type"],
                "agreement": review["verdict"] == recommendation
                if recommendation is not None
                else None,
                "reviewed_at": review["reviewed_at"],
                "started_at": review["started_at"],
            },
            at=review["reviewed_at"],
            node="review",
            event_id=review["event_id"],
        )
    for result in task.get("evaluations", []):
        for check, value, threshold in [
            ("schema", result["schema"] == "pass", 1),
            ("agreement", result.get("agreement"), 1),
            ("faithfulness", result.get("faithfulness"), 0.9),
        ]:
            if result["status"] == "not-evaluated" and check != "schema":
                continue
            emit(
                "evaluation",
                [result["evaluation_id"], check],
                {
                    "suite_id": NOTE_SUITE["suite_id"],
                    "case_id": d["decision_id"],
                    "metric_id": check,
                    "score": float(value) if value is not None else None,
                    "threshold": threshold,
                    "status": "error"
                    if value is None
                    else ("pass" if value >= threshold else "fail"),
                    "judge_model_version": config["judge_model"]["model"]
                    if check != "schema"
                    else None,
                    "judge_prompt_version": config["judge_model"]["prompt_version"]
                    if check != "schema"
                    else None,
                    "supporting_references": [],
                },
                at=result["occurred_at"],
                node="evaluate",
            )
    return events


def domain_outcome(decision, *, label, amount_minor, threshold):
    """Oracle use stays inside the workload; only derived contributions cross OTLP."""
    from reckoner.baseline.evaluate import evaluate_case

    outcome = decision["outcome"]
    result = evaluate_case(
        outcome,
        label,
        amount_minor,
        Decimal(threshold["review_cost"]),
        Decimal(threshold["margin_rate"]),
    )
    return (
        result
        | {k: decision[k] for k in ("tenant_id", "run_id", "task_id")}
        | {
            "occurred_at": decision["completed_at"],
            "rate_contributions": {
                "false_positive": {
                    "numerator": int(label == "legitimate" and outcome == "auto-decline"),
                    "denominator": int(label == "legitimate"),
                },
                "missed_fraud": {
                    "numerator": int(label == "fraud" and outcome == "auto-approve"),
                    "denominator": int(label == "fraud"),
                },
                "escalation": {"numerator": int(outcome == "escalate"), "denominator": 1},
            },
        }
    )


def reconcile_report(local_report: dict, warehouse_report: dict, expected: dict) -> dict:
    """Compare independent literal expected projections, including identities/generation.

    Exact string money is compared as Decimal; missing/null never becomes zero.
    Additional report metadata is allowed, but every expected path must be present.
    """
    mismatches = []

    def compare(actual, wanted, path):
        if isinstance(wanted, dict):
            if not isinstance(actual, dict):
                mismatches.append(path)
                return
            for key, value in wanted.items():
                if key not in actual:
                    mismatches.append(path + "." + key)
                else:
                    compare(actual[key], value, path + "." + key)
        elif isinstance(wanted, list):
            if not isinstance(actual, list) or len(actual) != len(wanted):
                mismatches.append(path)
                return
            for i, value in enumerate(wanted):
                compare(actual[i], value, f"{path}[{i}]")
        else:
            equal = actual == wanted
            if isinstance(wanted, str) and isinstance(actual, str):
                try:
                    equal = Decimal(actual) == Decimal(wanted)
                except ArithmeticError:
                    pass
            if not equal:
                mismatches.append(path)

    compare(local_report, expected, "local")
    compare(warehouse_report, expected, "warehouse")
    return {"matched": not mismatches, "mismatches": mismatches}


OFFLINE_SCORING_PURPOSES = {"pilot", "calibration", "development", "validation", "graph-comparison"}


def scoring_events(run: dict, task: dict) -> list[dict]:
    """Actual score-only executions; never manufacture a routing decision/outcome."""
    m = run["manifest"]
    if run.get("telemetry_mode") != "scoring-only" or m["purpose"] not in OFFLINE_SCORING_PURPOSES:
        raise ValueError("explicit offline score-only purpose required")
    identity = [m["tenant_id"], m["run_id"], task["task_id"]]
    trace = content_id(["trace", *identity])[:32]
    reproduction = {
        "experiment_version": m["experiment_id"],
        "cohort_version": m["cohort_version"],
        "config_version": m["config_id"],
        "code_revision": m["code_revision"],
        "dataset_version": m["dataset_version"],
        "prompt_version": None,
        "model_version": run["config"]["scorer"]["model"],
        "scorer_version": run["config"]["scorer"]["question_version"],
        "graph_version": run["config"].get("graph_version"),
    }
    events = []

    def emit(kind, key, payload, at, node="scoring"):
        eid = content_id([*identity, kind, key])
        doc = {
            "schema_version": "measurement-v1",
            "event_id": eid,
            "tenant_id": m["tenant_id"],
            "workflow_id": "reckoner",
            "workflow_version": "reckoner-v1",
            "run_id": m["run_id"],
            "task_id": task["task_id"],
            "trace_id": trace,
            "span_id": content_id(["span", eid])[:16],
            "node_name": node,
            "event_kind": kind,
            "occurred_at": at,
            "simulated": False,
            "reproducibility": reproduction,
            "payload": payload,
        }
        validate_document(doc, SCHEMAS / "measurement-v1.schema.json")
        events.append(doc)

    emit(
        "work_declaration", "root", {"child_task_ids": [], "evaluation_suites": []}, m["created_at"]
    )
    calls = task.get("calls", [])
    if calls:
        start = min(c["started_at"] for c in calls)
        terminal = task.get("protocols_closed", False)
        end = max(c["occurred_at"] for c in calls) if terminal else None
        emit(
            "execution",
            "terminal" if terminal else "started",
            {
                "started_at": start,
                "ended_at": end,
                "duration_ms": _duration(start, end) if end else None,
                "status": "completed" if terminal else "started",
                "attempt_number": 1,
                "parent_task_id": None,
                "parent_span_id": None,
                "provider": None,
                "model": None,
            },
            end or start,
        )
    for call in calls:
        if call.get("pending"):
            continue
        usage = call.get("usage") or {}
        amount = call["cost"]
        emit(
            "provider_usage",
            call["call_id"],
            {
                "provider": call["provider"],
                "model": call["model"],
                "call_id": call["call_id"],
                "input_tokens": _tokens(usage.get("input_tokens")),
                "output_tokens": _tokens(usage.get("output_tokens")),
                "cached_tokens": _tokens(usage.get("cached_tokens")),
                "cost_scope": "offline",
                "cost_status": "actual" if amount is not None else "unavailable",
                "cost_amount": format(Decimal(amount), "f") if amount is not None else None,
                "currency": "USD" if amount is not None else None,
                "price_table_version": call["price_table_version"],
            },
            call["occurred_at"],
            "provider." + call["provider"],
        )
    return events

"""Experiment verification: no incomplete, unpriced or unsettled run reports complete.

`verify_experiment` compares a claimed report with facts collected from protected
operational records. Any gap blocks a complete/pass claim; it never repairs or
reinterprets the facts. Fixture executions can never satisfy a measured gate.
"""

from collections import defaultdict
from decimal import Decimal

TERMINAL = {"scored", "decided", "noted"}


def _key(item):
    return (item["tenant_id"], item["run_id"], item["task_id"])


def verify_experiment(protocol: dict, run: dict, report: dict) -> dict:
    blockers = []

    def block(code, detail):
        blockers.append({"code": code, "detail": detail})

    if run.get("protocol_sha256") != protocol["protocol_sha256"]:
        block("protocol-mismatch", "facts were collected for another protocol")
    expected = {_key(case): case for case in protocol["cases"]}
    tasks = {}
    for task in run.get("tasks", []):
        key = _key(task)
        if key not in expected:
            moved = any(other[1:] == key[1:] for other in expected)
            block("tenant-mismatch" if moved else "unexpected-task", list(key))
            continue
        tasks[key] = task
        case = expected[key]
        if task["transaction_id"] != case["transaction_id"]:
            block("case-mismatch", list(key))
        if task["evidence_id"] != case["evidence_id"]:
            block("evidence-mismatch", list(key))
        if task.get("calibration_id") != report.get("calibration_id"):
            block("calibration-mismatch", list(key))
    missing = [k for k in expected if k not in tasks or tasks[k]["status"] not in TERMINAL]
    unstaged = [
        k for k, t in tasks.items() if any(s["status"] == "missing" for s in t.get("stages", []))
    ]
    if unstaged:
        block("missing-stage", f"{len(unstaged)} cases lack a declared note/judge/score stage")
    failed = [k for k, t in tasks.items() if t["status"] == "failed"]
    if missing:
        block("incomplete-expected-tasks", f"{len(missing)} of {len(expected)} expected cases")
    if failed:
        block("failed-calls", f"{len(failed)} cases ended without a valid provider result")
    calls = [call for task in tasks.values() for call in task["calls"]]
    price = (protocol.get("prices") or {}).get("price_table_version")
    if any(c["billing_status"] != "settled" or c["cost"] is None for c in calls):
        block("unknown-billing", "unsettled or uncertain provider cost remains")
    if any(c["cost"] is not None and (not price or c["price_table_id"] != price) for c in calls):
        block("missing-price", "a settled cost lacks the protocol's pinned price table")
    if report.get("attempts") != len(calls):
        block("retries-uncounted", f"{len(calls)} actual attempts")
    if report.get("latency_source") == "live" and any(c["latency_source"] != "live" for c in calls):
        block("replay-latency-as-live", "replayed or reused latency reported as live")
    if any(t.get("note_status") == "pending" for t in tasks.values()):
        block("note-pending", "required note work or billing is still pending")
    if run.get("stopped"):
        block("over-budget-stop", run.get("stop_reason") or "dispatch stopped at a limit")
    if run.get("execution_kind") != "measured" and report.get("measured"):
        block("fixture-claimed-as-measured", "fixture transports are not measured evidence")
    status = "complete" if not blockers else "incomplete"
    passed = (
        status == "complete"
        and report.get("passed") is True
        and run.get("execution_kind") == "measured"
    )
    false_claims = []
    if report.get("status") == "complete" and status != "complete":
        false_claims.append("status")
    if report.get("passed") is True and not passed:
        false_claims.append("passed")
    settled = sum((Decimal(c["cost"]) for c in calls if c["cost"] is not None), Decimal(0))
    return {
        "protocol_sha256": protocol["protocol_sha256"],
        "status": status,
        "passed": passed,
        "blockers": blockers,
        "false_claims": false_claims,
        "attempts": len(calls),
        "settled_usd": format(settled, "f"),
        "execution_kind": run.get("execution_kind"),
    }


def stage_name(dispatch: dict) -> str:
    if dispatch.get("derivation"):
        return dispatch["derivation"]["stage"]
    return {"online-note": "note", "judge": "judge"}.get(dispatch["purpose"], "score")


def recorded_execution_kind(repo, protocol_sha256: str) -> str:
    """The transport label persisted when the protocol was first executed."""
    row = repo._connection.execute(
        "SELECT execution_kind FROM reckoner.v1_protocol_executions WHERE protocol_sha256=%s",
        (protocol_sha256,),
    ).fetchone()
    return row["execution_kind"] if row else "unexecuted"


def _stage_status(calls):
    if not calls:
        return "missing"
    last = calls[-1]["attempt_status"]
    if last == "responded":
        return "complete"
    return "failed" if last in {"failed", "invalid"} else "uncertain"


def collect_run_facts(repo, protocol: dict) -> dict:
    """Per-case, per-stage facts for the protocol's own dispatch envelopes only."""
    from reckoner.v1.experiment.protocol import NOTES_DEFERRED

    ids = [d["protocol_id"] for d in protocol["dispatch"]]
    rows = repo._connection.execute(
        "SELECT c.call_id,c.tenant_id,c.run_id,c.task_id,c.protocol_id,c.document,"
        "c.dispatched_at,r.received_at,r.score,r.category,s.cost,s.status AS settled,"
        "u.status AS uncertain FROM reckoner.v1_provider_calls c "
        "LEFT JOIN reckoner.v1_provider_responses r USING (call_id) "
        "LEFT JOIN reckoner.v1_settlements s ON s.call_id=c.call_id AND s.status='settled' "
        "LEFT JOIN reckoner.v1_settlements u ON u.call_id=c.call_id AND u.status='uncertain' "
        "WHERE c.protocol_id = ANY(%s) ORDER BY c.dispatched_at,c.call_id",
        (ids,),
    ).fetchall()
    # Requests are keyed by envelope: a stage never overwrites another stage's request.
    expected = {
        (d["protocol_id"], t["task_id"]): t["request_sha256"]
        for d in protocol["dispatch"]
        for t in d["tasks"]
    }
    by_stage = defaultdict(list)
    for row in rows:
        score = row["score"] or {}
        if "attempt_status" in score:
            attempt = score["attempt_status"]
            price = (score.get("cost") or {}).get("price_table_id")
        else:
            attempt = score.get("status", "uncertain") if row["score"] else "unanswered"
            price = score.get("price_table_id")
        wanted = expected.get((row["protocol_id"], row["task_id"]))
        document = row["document"]
        matches = (
            document["request_sha256"] == wanted
            if wanted is not None
            else isinstance(document.get("derived_from"), dict)
        )
        timed = row["received_at"] is not None and row["dispatched_at"] is not None
        by_stage[(row["protocol_id"], row["tenant_id"], row["run_id"], row["task_id"])].append(
            {
                "call_id": row["call_id"],
                "protocol_id": row["protocol_id"],
                "attempt_status": attempt,
                "billing_status": "settled"
                if row["settled"]
                else ("uncertain" if row["uncertain"] else "missing"),
                "cost": format(row["cost"], "f") if row["cost"] is not None else None,
                "price_table_id": price,
                # Every call here was dispatched under this protocol; its latency is live
                # only when both persisted timestamps exist.
                "latency_source": "live" if timed else "unavailable",
                "latency_ms": (row["received_at"] - row["dispatched_at"]).total_seconds() * 1000
                if timed
                else None,
                "request_sha256": document["request_sha256"],
                "request_matches": matches,
            }
        )
    closed = {
        r["protocol_id"]
        for r in repo._connection.execute(
            "SELECT protocol_id FROM reckoner.v1_protocol_closures WHERE protocol_id = ANY(%s)",
            (ids,),
        ).fetchall()
    }
    overage = repo._connection.execute(
        "SELECT EXISTS (SELECT 1 FROM reckoner.v1_provider_calls c JOIN reckoner.v1_settlements s "
        "USING (call_id) WHERE c.protocol_id = ANY(%s) AND (s.cost > c.maximum_cost OR "
        "(s.usage->>'input_tokens')::numeric > (c.document->>'input_token_ceiling')::numeric))"
        " AS overage",
        (ids,),
    ).fetchone()["overage"]
    workflow = protocol["telemetry_mode"] == "workflow"
    tasks = []
    for case in protocol["cases"]:
        key = _key(case)
        stages, calls = [], []
        for dispatch in protocol["dispatch"]:
            if (dispatch["tenant_id"], dispatch["run_id"]) != key[:2] or key[2] not in {
                t["task_id"] for t in dispatch["tasks"]
            }:
                continue
            stage_calls = by_stage.get((dispatch["protocol_id"], *key), [])
            calls += stage_calls
            stages.append(
                {
                    "stage": stage_name(dispatch),
                    "protocol_id": dispatch["protocol_id"],
                    "status": _stage_status(stage_calls),
                }
            )
        statuses = {s["status"] for s in stages}
        if statuses == {"missing"}:
            status = "missing"
        elif "failed" in statuses:
            status = "failed"
        elif "uncertain" in statuses:
            status = "uncertain"
        elif "missing" in statuses:
            status = "incomplete-stages"
        else:
            status = "noted" if protocol["provider"] == "anthropic" else "scored"
        task = {
            **{k: case[k] for k in ("tenant_id", "run_id", "task_id", "transaction_id")},
            "evidence_id": None
            if not calls
            else (
                "request-mismatch"
                if not all(c["request_matches"] for c in calls)
                else case["evidence_id"]
            ),
            "calibration_id": None,
            "note_status": None,
            "status": status,
            "stages": stages,
            "calls": calls,
        }
        identity = {k: case[k] for k in ("tenant_id", "run_id", "task_id")}
        decision = repo.workflow_document("v1_decisions", identity) if workflow else None
        if decision is not None:
            note = repo.workflow_document(
                "v1_note_results",
                {"tenant_id": case["tenant_id"], "case_id": decision["decision_id"]},
            ) or repo.workflow_document(
                "v1_note_work", {"tenant_id": case["tenant_id"], "case_id": decision["decision_id"]}
            )
            if protocol["provider"] == "typesafe":
                binding = repo.workflow_document("v1_workflow_tasks", identity)
                task["status"] = "decided"
                task["evidence_id"] = decision["evidence_id"]
                task["calibration_id"] = binding["calibration_id"] if binding else None
                if decision["outcome"] == "escalate":
                    task["note_status"] = (
                        "deferred"
                        if protocol["purpose"] in NOTES_DEFERRED
                        else (note["status"] if note else "pending")
                    )
            elif any(s["stage"] == "note" for s in stages):
                task["note_status"] = note["status"] if note else "pending"
                if task["note_status"] not in {"succeeded", "pending"}:
                    task["status"] = "failed"
        tasks.append(task)
    stopped = bool(closed) and any(not t["calls"] for t in tasks) and not workflow
    return {
        "protocol_sha256": protocol["protocol_sha256"],
        "execution_kind": recorded_execution_kind(repo, protocol["protocol_sha256"]),
        "stopped": stopped or bool(overage),
        "stop_reason": "provider overage requires reconciliation"
        if overage
        else ("dispatch closed before every case ran" if stopped else None),
        "closed": sorted(closed),
        "tasks": tasks,
    }

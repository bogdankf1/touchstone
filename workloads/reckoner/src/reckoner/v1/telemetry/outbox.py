"""Transaction-aware generic events, separate from immutable operational review bytes."""

from psycopg.types.json import Jsonb

from reckoner.v1.telemetry.exporter import encode


def enqueue_events(repo, events: list[dict]) -> int:
    inserted = 0
    with repo._connection.transaction():
        for event in events:
            payload = encode(event)
            cursor = repo._connection.execute(
                "INSERT INTO reckoner.v1_otlp_delivery "
                "(tenant_id,event_id,run_id,task_id,document,payload) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    event["tenant_id"],
                    event["event_id"],
                    event["run_id"],
                    event.get("task_id"),
                    Jsonb(event),
                    payload,
                ),
            )
            inserted += cursor.rowcount
            existing = repo._connection.execute(
                "SELECT document,payload FROM reckoner.v1_otlp_delivery WHERE tenant_id=%s "
                "AND event_id=%s",
                (event["tenant_id"], event["event_id"]),
            ).fetchone()
            if existing["document"] != event or bytes(existing["payload"]) != payload:
                raise ValueError("immutable OTLP event conflict")
    return inserted


def export_pending(repo, exporter, *, limit: int) -> dict:
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("limit must be 1..10000")
    sent = rejected = 0
    with repo._connection.transaction():
        rows = repo._connection.execute(
            "SELECT tenant_id,event_id,payload FROM reckoner.v1_otlp_delivery WHERE "
            "delivered_at IS NULL ORDER BY created_at,event_id LIMIT %s FOR UPDATE SKIP "
            "LOCKED",
            (limit,),
        ).fetchall()
        for row in rows:
            result = exporter.export(bytes(row["payload"]))
            rejected += result["rejected_spans"]
            if not result["accepted"]:
                break
            repo._connection.execute(
                "UPDATE reckoner.v1_otlp_delivery SET delivered_at=clock_timestamp() WHERE "
                "tenant_id=%s AND event_id=%s",
                (row["tenant_id"], row["event_id"]),
            )
            sent += 1
        pending = repo._connection.execute(
            "SELECT count(*) AS n FROM reckoner.v1_otlp_delivery WHERE delivered_at IS NULL"
        ).fetchone()["n"]
    return {"sent": sent, "pending": pending, "rejected_spans": rejected}


def _bundle(repo, tenant_id, run_id, task_id):
    from reckoner.v1.telemetry.events import cost_scope

    run = repo._connection.execute(
        "SELECT r.document AS manifest,c.document AS config,r.telemetry_mode "
        "FROM reckoner.v1_runs r "
        "JOIN reckoner.v1_configs c USING(tenant_id,config_id) WHERE r.tenant_id=%s AND "
        "r.run_id=%s",
        (tenant_id, run_id),
    ).fetchone()
    decision = repo.workflow_document(
        "v1_decisions", {"tenant_id": tenant_id, "run_id": run_id, "task_id": task_id}
    )
    if run is None or (decision is None and run["telemetry_mode"] != "scoring-only"):
        raise ValueError("pending root decision")
    rows = repo._connection.execute(
        "SELECT c.call_id,c.provider,c.purpose,c.document,c.dispatched_at,r.score,r.received_"
        "at,s.usage,s.cost "
        "FROM reckoner.v1_provider_calls c LEFT JOIN reckoner.v1_provider_responses r "
        "USING(tenant_id,call_id) "
        "LEFT JOIN LATERAL (SELECT usage,cost FROM reckoner.v1_settlements s WHERE "
        "s.tenant_id=c.tenant_id AND s.call_id=c.call_id ORDER BY (status='settled') DESC "
        "LIMIT 1) s ON true "
        "WHERE c.tenant_id=%s AND c.run_id=%s AND c.task_id=%s ORDER BY c.call_id",
        (tenant_id, run_id, task_id),
    ).fetchall()
    calls = []
    for row in rows:
        cost_scope(row["purpose"])
        config_key = (
            "scorer"
            if row["provider"] == "typesafe"
            else ("judge_model" if row["purpose"] == "judge" else "note_model")
        )
        calls.append(
            {
                "call_id": row["call_id"],
                "provider": row["provider"],
                "purpose": row["purpose"],
                "model": row["document"]["model"],
                "cost": row["cost"],
                "usage": row["usage"],
                "price_table_version": run["config"][config_key]["price_table"][
                    "price_table_version"
                ],
                "occurred_at": (row["received_at"] or row["dispatched_at"]).isoformat(),
                "started_at": row["dispatched_at"].isoformat(),
                "pending": row["received_at"] is None,
            }
        )
    if decision is None:
        closed = (
            repo._connection.execute(
                "SELECT count(*) AS n FROM reckoner.v1_provider_calls c WHERE tenant_id=%s "
                "AND run_id=%s AND task_id=%s "
                "AND NOT EXISTS (SELECT 1 FROM reckoner.v1_protocol_closures p WHERE "
                "p.protocol_id=c.protocol_id)",
                (tenant_id, run_id, task_id),
            ).fetchone()["n"]
            == 0
        )
        return run, {
            "scoring": True,
            "task_id": task_id,
            "calls": calls,
            "protocols_closed": bool(calls) and closed and not any(c["pending"] for c in calls),
        }
    note = repo.workflow_document(
        "v1_note_results", {"tenant_id": tenant_id, "case_id": decision["decision_id"]}
    )
    reviews = [
        r["document"]
        for r in repo._connection.execute(
            "SELECT document FROM reckoner.v1_outbox WHERE tenant_id=%s AND run_id=%s AND "
            "task_id=%s "
            "AND document->>'schema_version'='reckoner-review-outbox-v1'",
            (tenant_id, run_id, task_id),
        ).fetchall()
    ]
    evaluations = []
    for r in repo._connection.execute(
        "SELECT document,recorded_at FROM reckoner.v1_note_evaluations WHERE tenant_id=%s AND"
        " run_id=%s",
        (tenant_id, run_id),
    ).fetchall():
        for case in r["document"]["cases"]:
            if case["case_id"] == decision["decision_id"]:
                evaluations.append(
                    case
                    | {
                        "evaluation_id": r["document"]["evaluation_id"],
                        "occurred_at": r["recorded_at"].isoformat(),
                    }
                )
    return run, {
        "decision": decision,
        "calls": calls,
        "note_result": note,
        "reviews": reviews,
        "evaluations": evaluations,
    }


def _require_mode(repo, tenant_id, run_id, expected):
    row = repo._connection.execute(
        "SELECT telemetry_mode FROM reckoner.v1_runs WHERE tenant_id=%s AND run_id=%s",
        (tenant_id, run_id),
    ).fetchone()
    if row is None or row["telemetry_mode"] != expected:
        raise ValueError("collector/run telemetry mode mismatch")


def collect_run(repo, tenant_id: str, run_id: str, *, outcomes: list[dict] = ()) -> int:
    """Map persisted immutable sources; retries preserve IDs and Task8 source bytes."""
    from reckoner.v1.telemetry.events import measurement_events

    _require_mode(repo, tenant_id, run_id, "workflow")
    inserted = 0
    with repo._connection.transaction():
        tasks = repo._connection.execute(
            "SELECT task_id FROM reckoner.v1_decisions WHERE tenant_id=%s AND run_id=%s ORDER"
            " BY task_id",
            (tenant_id, run_id),
        ).fetchall()
        for task in tasks:
            run, bundle = _bundle(repo, tenant_id, run_id, task["task_id"])
            selected = [
                o
                for o in outcomes
                if (o["tenant_id"], o["run_id"], o["task_id"])
                == (tenant_id, run_id, task["task_id"])
            ]
            inserted += enqueue_events(repo, measurement_events(run, bundle, selected))
    return inserted


def close_scope(repo, *, tenant_id: str, run_id: str, task_id: str, scope: str) -> int:
    """Close only terminal work with exact settled calls; SQL serializes new dispatches."""
    from reckoner.contracts import content_id
    from reckoner.v1.telemetry.events import cost_scope, measurement_events

    if scope not in {"online", "offline"}:
        raise ValueError("unknown cost scope")
    with repo._connection.transaction():
        repo._connection.execute(
            "SELECT task_id FROM reckoner.v1_tasks WHERE tenant_id=%s AND run_id=%s AND "
            "task_id=%s FOR UPDATE",
            (tenant_id, run_id, task_id),
        ).fetchone()
        run, task = _bundle(repo, tenant_id, run_id, task_id)
        if task.get("scoring") and not task["protocols_closed"]:
            raise ValueError("pending source protocol or response")
        escalated = not task.get("scoring") and task["decision"]["outcome"] == "escalate"
        if escalated and task["note_result"] is None:
            raise ValueError("pending note work")
        if (
            scope == "offline"
            and escalated
            and not any(r["status"] in {"pass", "fail", "error"} for r in task["evaluations"])
        ):
            raise ValueError("pending evaluation work")
        calls = [c for c in task["calls"] if cost_scope(c["purpose"]) == scope]
        if any(c["cost"] is None for c in calls):
            raise ValueError("pending or uncertain billing")
        events = measurement_events(run, task, [])
        base = events[0]
        event_id = content_id([tenant_id, run_id, task_id, "work_closure", scope])
        closure = base | {
            "event_id": event_id,
            "span_id": content_id(["span", event_id])[:16],
            "event_kind": "work_closure",
            "node_name": "accounting",
            "payload": {
                "cost_scope": scope,
                "call_ids": sorted(c["call_id"] for c in calls),
                "work_status": "completed",
                "billing_status": "complete",
            },
        }
        return enqueue_events(repo, [*events, closure])


def evaluate_run(repo, tenant_id: str, run_id: str) -> int:
    """Evaluator-role only: emit derived business outcomes from the pinned oracle."""
    from reckoner.v1.telemetry.events import domain_outcome

    rows = repo._connection.execute(
        "SELECT d.document AS decision,o.label,x.document->>'amount_minor' AS "
        "amount_minor,t.document AS thresholds "
        "FROM reckoner.v1_decisions d JOIN reckoner.v1_configs c USING(tenant_id,config_id) "
        "JOIN reckoner.threshold_configs t ON t.tenant_id=c.tenant_id AND "
        "t.config_id=c.threshold_config_id "
        "JOIN reckoner.transactions x ON x.tenant_id=d.tenant_id AND "
        "x.transaction_id=d.transaction_id "
        "JOIN oracle.oracle_labels o ON o.tenant_id=d.tenant_id AND "
        "o.transaction_id=d.transaction_id "
        "AND o.oracle_version='cctd-label-v1' WHERE d.tenant_id=%s AND d.run_id=%s",
        (tenant_id, run_id),
    ).fetchall()
    outcomes = [
        domain_outcome(
            row["decision"],
            label=row["label"],
            amount_minor=int(row["amount_minor"]),
            threshold=row["thresholds"]["parameters"],
        )
        for row in rows
    ]
    # Evaluation role emits only its own derived outcome/contribution records.
    from reckoner.v1.telemetry.events import measurement_events

    total = 0
    for result in outcomes:
        run = repo._connection.execute(
            "SELECT r.document AS manifest,c.document AS config,r.telemetry_mode "
            "FROM reckoner.v1_runs r "
            "JOIN reckoner.v1_configs c USING(tenant_id,config_id) WHERE r.tenant_id=%s AND "
            "r.run_id=%s",
            (tenant_id, run_id),
        ).fetchone()
        decision = next(
            row["decision"] for row in rows if row["decision"]["task_id"] == result["task_id"]
        )
        events = measurement_events(run, {"decision": decision}, [result])
        total += enqueue_events(
            repo, [e for e in events if e["event_kind"] in {"outcome", "metric_contribution"}]
        )
    return total


def collect_scoring_run(repo, tenant_id: str, run_id: str) -> int:
    """Explicit Task4 offline handoff; close source protocols before final telemetry closure."""
    from reckoner.v1.telemetry.events import scoring_events

    _require_mode(repo, tenant_id, run_id, "scoring-only")
    inserted = 0
    with repo._connection.transaction():
        tasks = repo._connection.execute(
            "SELECT task_id FROM reckoner.v1_tasks WHERE tenant_id=%s AND run_id=%s ORDER BY "
            "task_id",
            (tenant_id, run_id),
        ).fetchall()
        for row in tasks:
            run, task = _bundle(repo, tenant_id, run_id, row["task_id"])
            if not task.get("scoring"):
                raise ValueError("score-only export cannot replace a routing execution")
            inserted += enqueue_events(repo, scoring_events(run, task))
    return inserted

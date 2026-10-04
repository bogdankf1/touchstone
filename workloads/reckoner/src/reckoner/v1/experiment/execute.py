"""Sequential execution of one recorded, approved protocol under stop-on-limit.

Only a protocol whose exact SHA-256 was recorded with an external approval (and
whose full envelopes were reserved) can run. Every case is preflighted before the
first dispatch; each request must be the protocol's exact request. A budget or
overage stop leaves the remaining cases undispatched and incomplete. Raw outputs and
request/response hashes are retained in a new ignored evidence directory.

`execution_kind` labels the transport: `fixture` for deterministic test
transports (never Jev/Anthropic or measured evidence), `measured` for an approved
real provider run. No provider key is read here; callers inject clients.
"""

import json
import os
from decimal import Decimal
from pathlib import Path

from reckoner.contracts import content_id
from reckoner.storage.budget import BudgetExceeded
from reckoner.v1.experiment.protocol import validate_body
from reckoner.v1.storage.budget import ProviderBudget
from reckoner.v1.storage.repository import V1Repository

KINDS = {"fixture", "measured"}


def load_recorded(repo, protocol_id: str) -> dict:
    """The immutable protocol body recorded with its approval, or a refusal."""
    row = repo._connection.execute(
        "SELECT x.document, a.document AS approval FROM reckoner.v1_experiment_protocols x "
        "JOIN reckoner.v1_protocol_approvals a USING (protocol_sha256) "
        "WHERE x.protocol_sha256=%s",
        (protocol_id,),
    ).fetchone()
    if row is None:
        raise ValueError("protocol has no recorded approval; nothing dispatched")
    protocol = row["document"]
    validate_body(protocol)
    if protocol["protocol_sha256"] != protocol_id:
        raise ValueError("recorded protocol identity mismatch")
    authorized = {
        r["protocol_id"]
        for r in repo._connection.execute(
            "SELECT protocol_id FROM reckoner.v1_protocol_authorizations "
            "WHERE protocol_sha256=%s AND approval_id=%s",
            (protocol_id, row["approval"]["approval_id"]),
        ).fetchall()
    }
    if authorized != {d["protocol_id"] for d in protocol["dispatch"]}:
        raise ValueError("dispatch envelopes are not all recorded under the approval")
    return protocol


def _output(path):
    path = Path(path)
    if path.exists() and any(path.iterdir()):
        raise FileExistsError("evidence output directory must be new; never overwrite")
    (path / "raw").mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    os.chmod(path / "raw", 0o700)
    return path


def _write(path, text):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)


def retain_outputs(repo, protocol, directory: Path) -> dict:
    """Raw bodies (0600) plus request/response hashes for each actual attempt."""
    ids = [d["protocol_id"] for d in protocol["dispatch"]]
    rows = repo._connection.execute(
        "SELECT c.call_id,c.tenant_id,c.run_id,c.task_id,c.protocol_id,c.document,"
        "c.dispatched_at,r.category,r.body,r.received_at,s.usage,s.cost,s.status "
        "FROM reckoner.v1_provider_calls c "
        "LEFT JOIN reckoner.v1_provider_responses r USING (call_id) "
        "LEFT JOIN reckoner.v1_settlements s ON s.call_id=c.call_id "
        "AND s.status=(SELECT max(x.status) FROM reckoner.v1_settlements x "
        "WHERE x.call_id=c.call_id) "
        "WHERE c.protocol_id = ANY(%s) ORDER BY c.dispatched_at,c.call_id",
        (ids,),
    ).fetchall()
    lines = []
    for row in rows:
        request = row["document"].get("request_document")
        record = {
            "call_id": row["call_id"],
            "tenant_id": row["tenant_id"],
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "protocol_id": row["protocol_id"],
            "request_sha256": row["document"]["request_sha256"],
            "response_sha256": content_id(row["body"]) if row["body"] is not None else None,
            "category": row["category"],
            "usage": row["usage"],
            "cost": format(row["cost"], "f") if row["cost"] is not None else None,
            "billing_status": row["status"] or "missing",
            "dispatched_at": row["dispatched_at"].isoformat(),
            "received_at": row["received_at"].isoformat() if row["received_at"] else None,
        }
        lines.append(json.dumps(record, sort_keys=True))
        raw = {"call_id": row["call_id"], "request": request, "response": row["body"]}
        _write(directory / "raw" / f"{content_id([row['call_id']])}.json", json.dumps(raw))
    _write(directory / "calls.jsonl", "\n".join(lines) + ("\n" if lines else ""))
    return {"calls": len(rows), "path": str(directory)}


def _cases(protocol):
    return {(c["tenant_id"], c["run_id"], c["task_id"]): c for c in protocol["cases"]}


def _score_cases(repo, protocol, client):
    from reckoner.v1.storage.attempts import _preflight, score_task

    cases = _cases(protocol)
    prepared = []
    for dispatch in sorted(protocol["dispatch"], key=lambda d: (d["tenant_id"], d["run_id"])):
        for item in dispatch["tasks"]:
            key = (dispatch["tenant_id"], dispatch["run_id"], item["task_id"])
            task = repo.task(*key)
            evidence = repo.workflow_document(
                "v1_evidence", {"tenant_id": key[0], "evidence_id": cases[key]["evidence_id"]}
            )
            if evidence is None:
                raise ValueError("pinned prepared evidence is missing; nothing dispatched")
            # Exact request/case/model/pricing/bounds are checked for every case first.
            _preflight(repo, task, evidence, dispatch)
            prepared.append((key, task, evidence, dispatch))
    results, stop = [], None
    for key, task, evidence, dispatch in prepared:
        if stop is not None:
            results.append({"key": key, "status": "not-dispatched"})
            continue
        try:
            score = score_task(repo, client, task, evidence, dispatch)
        except BudgetExceeded as error:
            stop = str(error)
            results.append({"key": key, "status": "not-dispatched"})
            continue
        results.append({"key": key, "status": score.get("attempt_status") or "deferred"})
    return results, stop


def _workflow_cases(repo, protocol, client, dsn, settings):
    from reckoner.v1.storage.checkpoints import PostgresCheckpointer
    from reckoner.v1.workflow import build_graph, run_task

    cases = _cases(protocol)
    repo.scorer_client = client
    results, stop = [], None
    for dispatch in sorted(protocol["dispatch"], key=lambda d: (d["tenant_id"], d["run_id"])):
        graph = build_graph(PostgresCheckpointer(dsn, dispatch["tenant_id"]))
        for item in dispatch["tasks"]:
            key = (dispatch["tenant_id"], dispatch["run_id"], item["task_id"])
            if stop is not None:
                results.append({"key": key, "status": "not-dispatched"})
                continue
            task = repo.task(*key)
            config = {
                "evidence_id": cases[key]["evidence_id"],
                "evidence_mode": cases[key]["evidence_mode"],
                "data_kind": settings["data_kind"],
                "protocol": dispatch,
                "calibration": settings["calibration"],
            }
            try:
                decision = run_task(repo, graph, task, config)
            except BudgetExceeded as error:
                stop = str(error)
                results.append({"key": key, "status": "not-dispatched"})
                continue
            results.append({"key": key, "status": "decided", "outcome": decision["outcome"]})
    return results, stop


def _note_cases(repo, protocol, client):
    from reckoner.v1.notes.lifecycle import continue_note, note_work

    results, stop = [], None
    for dispatch in sorted(protocol["dispatch"], key=lambda d: (d["tenant_id"], d["run_id"])):
        for item in dispatch["tasks"]:
            key = (dispatch["tenant_id"], dispatch["run_id"], item["task_id"])
            if dispatch["purpose"] != "online-note":
                # Judge stages run through the evaluator's evaluate_note_fixtures.
                results.append({"key": key, "status": "evaluator-owned"})
                continue
            if stop is not None:
                results.append({"key": key, "status": "not-dispatched"})
                continue
            decision = repo.workflow_document(
                "v1_decisions", dict(zip(("tenant_id", "run_id", "task_id"), key, strict=True))
            )
            if decision is None or decision["outcome"] != "escalate":
                raise ValueError("note protocol case is not a persisted escalation")
            repo.note_client, repo.note_protocol = client, dispatch
            try:
                continue_note(repo, decision)
            except BudgetExceeded as error:
                stop = str(error)
                results.append({"key": key, "status": "not-dispatched"})
                continue
            results.append({"key": key, "status": note_work(repo, decision)["status"]})
    return results, stop


def _telemetry(repo, protocol, closed):
    from reckoner.v1.telemetry.outbox import close_scope, collect_scoring_run

    if protocol["telemetry_mode"] != "scoring-only":
        return {"collected": False, "reason": "workflow telemetry uses collect_run"}
    enqueued, pending = 0, []
    for run in protocol["runs"]:
        enqueued += collect_scoring_run(repo, run["tenant_id"], run["run_id"])
    if closed:
        for case in protocol["cases"]:
            for scope in ("online", "offline"):
                try:
                    enqueued += close_scope(
                        repo,
                        tenant_id=case["tenant_id"],
                        run_id=case["run_id"],
                        task_id=case["task_id"],
                        scope=scope,
                    )
                except ValueError as error:
                    pending.append(
                        {"task_id": case["task_id"], "scope": scope, "reason": str(error)}
                    )
    return {"collected": True, "enqueued": enqueued, "pending": pending}


def execute_protocol(
    protocol_id: str,
    *,
    provider_clients: dict,
    dsn: str,
    execution_kind: str,
    output_dir: Path,
    data_kind: str | None = None,
    calibration: dict | None = None,
) -> dict:
    """Run one recorded protocol sequentially; never beyond its cases, model or cap."""
    if execution_kind not in KINDS:
        raise ValueError("unknown execution kind; use fixture or measured")
    with V1Repository(dsn) as repo:
        protocol = load_recorded(repo, protocol_id)
        client = provider_clients.get(protocol["provider"])
        if client is None:
            raise ValueError("no provider client injected for this protocol; nothing dispatched")
        directory = _output(output_dir)
        if protocol["provider"] == "anthropic":
            results, stop = _note_cases(repo, protocol, client)
        elif protocol["telemetry_mode"] == "workflow":
            if data_kind not in {"fabricated", "simulated-cctd"}:
                raise ValueError("workflow execution needs an explicit data kind")
            results, stop = _workflow_cases(
                repo, protocol, client, dsn, {"data_kind": data_kind, "calibration": calibration}
            )
        else:
            results, stop = _score_cases(repo, protocol, client)
        terminal = {
            "responded",
            "failed",
            "uncertain",
            "decided",
            "succeeded",
            "invalid",
            "evaluator-owned",
        }
        resumable = any(r["status"] not in terminal | {"not-dispatched"} for r in results)
        closed = stop is not None or not resumable
        ledger = ProviderBudget(repo._connection)
        if closed:
            for dispatch in protocol["dispatch"]:
                ledger.close(dispatch["protocol_id"])
        telemetry = _telemetry(repo, protocol, closed)
        retained = retain_outputs(repo, protocol, directory)
        totals = repo._connection.execute(
            "SELECT COALESCE(sum(s.cost),0) AS settled, count(*) FILTER (WHERE s.cost IS NULL) "
            "AS uncertain FROM reckoner.v1_provider_calls c LEFT JOIN reckoner.v1_settlements s "
            "ON s.call_id=c.call_id AND s.status='settled' WHERE c.protocol_id = ANY(%s)",
            ([d["protocol_id"] for d in protocol["dispatch"]],),
        ).fetchone()
        complete = (
            stop is None
            and all(
                r["status"] in {"responded", "decided", "succeeded", "evaluator-owned"}
                for r in results
            )
            and totals["uncertain"] == 0
        )
        summary = {
            "protocol_sha256": protocol_id,
            "provider": protocol["provider"],
            "purpose": protocol["purpose"],
            "execution_kind": execution_kind,
            "status": "stopped" if stop else ("complete" if complete else "incomplete"),
            "stop_reason": stop,
            "closed": closed,
            "cases": [
                {
                    **dict(zip(("tenant_id", "run_id", "task_id"), r["key"], strict=True)),
                    **{k: v for k, v in r.items() if k != "key"},
                }
                for r in results
            ],
            "calls": retained["calls"],
            "settled_usd": format(Decimal(totals["settled"]), "f"),
            "uncertain_calls": totals["uncertain"],
            "telemetry": telemetry,
            "outputs": retained["path"],
            "dataset_simulated": True,
        }
        _write(directory / "summary.json", json.dumps(summary, sort_keys=True, indent=2))
        return summary

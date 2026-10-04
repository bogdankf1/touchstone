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
        "c.dispatched_at,r.category,r.body,r.received_at,"
        "COALESCE(k.usage,u.usage) AS usage,COALESCE(k.cost,u.cost) AS cost,"
        "COALESCE(k.status,u.status) AS status FROM reckoner.v1_provider_calls c "
        "LEFT JOIN reckoner.v1_provider_responses r USING (call_id) "
        # A later settled record supersedes an earlier uncertain one.
        "LEFT JOIN reckoner.v1_settlements k ON k.call_id=c.call_id AND k.status='settled' "
        "LEFT JOIN reckoner.v1_settlements u ON u.call_id=c.call_id AND u.status='uncertain' "
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


def _dispatch(prepared, run_one):
    """Sequential dispatch: a budget stop or any error halts every later case."""
    results, stop, error = [], None, None
    for key, *item in prepared:
        if stop is not None or error is not None:
            results.append({"key": key, "status": "not-dispatched"})
            continue
        try:
            results.append({"key": key, **run_one(*item)})
        except BudgetExceeded as exc:
            stop = str(exc)
            results.append({"key": key, "status": "not-dispatched"})
        except Exception as exc:  # recorded, outputs retained, then re-raised
            error = exc
            results.append({"key": key, "status": "error", "error": type(exc).__name__})
    return results, stop, error


def _ordered(protocol):
    for dispatch in sorted(protocol["dispatch"], key=lambda d: (d["tenant_id"], d["run_id"])):
        for item in dispatch["tasks"]:
            yield dispatch, (dispatch["tenant_id"], dispatch["run_id"], item["task_id"])


def _pinned_evidence(repo, cases, key):
    evidence = repo.workflow_document(
        "v1_evidence", {"tenant_id": key[0], "evidence_id": cases[key]["evidence_id"]}
    )
    if evidence is None:
        raise ValueError("pinned prepared evidence is missing; nothing dispatched")
    mode = "gds-augmented" if evidence.get("graph_projection") is not None else "relational"
    if mode != cases[key]["evidence_mode"]:
        raise ValueError("pinned evidence belongs to another arm; nothing dispatched")
    return evidence


def _score_cases(repo, protocol, client):
    from reckoner.v1.storage.attempts import _preflight, score_task

    cases = _cases(protocol)
    prepared = []
    for dispatch, key in _ordered(protocol):
        task = repo.task(*key)
        evidence = _pinned_evidence(repo, cases, key)
        # Exact request/case/model/pricing/bounds are checked for every case first.
        _preflight(repo, task, evidence, dispatch)
        prepared.append((key, task, evidence, dispatch))

    def run_one(task, evidence, dispatch):
        score = score_task(repo, client, task, evidence, dispatch)
        return {"status": score.get("attempt_status") or "deferred"}

    return _dispatch(prepared, run_one)


def _workflow_cases(repo, protocol, client, dsn, settings):
    from reckoner.v1.storage.attempts import _preflight
    from reckoner.v1.storage.checkpoints import PostgresCheckpointer
    from reckoner.v1.workflow import build_graph, run_task

    cases = _cases(protocol)
    repo.scorer_client = client
    graphs, prepared = {}, []
    for dispatch, key in _ordered(protocol):
        task = repo.task(*key)
        evidence = _pinned_evidence(repo, cases, key)
        if evidence["coverage"]["status"] == "available":
            _preflight(repo, task, evidence, dispatch)
        if key[0] not in graphs:
            graphs[key[0]] = build_graph(PostgresCheckpointer(dsn, key[0]))
        settings_for = {
            "evidence_id": cases[key]["evidence_id"],
            "evidence_mode": cases[key]["evidence_mode"],
            "data_kind": settings["data_kind"],
            "protocol": dispatch,
            "calibration": settings["calibration"],
        }
        prepared.append((key, task, graphs[key[0]], settings_for))

    def run_one(task, graph, settings_for):
        decision = run_task(repo, graph, task, settings_for)
        return {"status": "decided", "outcome": decision["outcome"]}

    return _dispatch(prepared, run_one)


def _note_cases(repo, protocol, client):
    from reckoner.v1.notes.calls import BudgetedCalls
    from reckoner.v1.notes.lifecycle import continue_note, note_work
    from reckoner.v1.notes.prompt import build_note_request

    prepared, judged = [], []
    for dispatch, key in _ordered(protocol):
        if dispatch["purpose"] != "online-note":
            # Judge stages run through the evaluator's evaluate_note_fixtures.
            judged.append({"key": key, "status": "evaluator-owned"})
            continue
        identity = dict(zip(("tenant_id", "run_id", "task_id"), key, strict=True))
        decision = repo.workflow_document("v1_decisions", identity)
        if decision is None or decision["outcome"] != "escalate":
            raise ValueError("note protocol case is not a persisted escalation")
        task = repo.task(*key)
        config = repo.workflow_document(
            "v1_configs", {"tenant_id": key[0], "config_id": task["config_id"]}
        )
        evidence = repo.workflow_document(
            "v1_evidence", {"tenant_id": key[0], "evidence_id": decision["evidence_id"]}
        )
        score = {}
        if decision["call_id"] is not None:
            score = repo._connection.execute(
                "SELECT score FROM reckoner.v1_provider_responses WHERE tenant_id=%s "
                "AND call_id=%s",
                (key[0], decision["call_id"]),
            ).fetchone()["score"]
        # Every note request is checked against its exact approved hash before any call.
        BudgetedCalls(repo, client, task, config, kind="note").preflight(
            build_note_request(decision, evidence, score, config), "note-generation", dispatch
        )
        prepared.append((key, decision, dispatch))

    def run_one(decision, dispatch):
        repo.note_client, repo.note_protocol = client, dispatch
        continue_note(repo, decision)
        return {"status": note_work(repo, decision)["status"]}

    results, stop, error = _dispatch(prepared, run_one)
    return results + judged, stop, error


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


def _record_kind(repo, protocol_id, execution_kind):
    """Persist the transport label once; a later run cannot relabel the protocol."""
    with repo._connection.transaction():
        repo._connection.execute(
            "INSERT INTO reckoner.v1_protocol_executions (protocol_sha256, execution_kind) "
            "VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (protocol_id, execution_kind),
        )
        recorded = repo._connection.execute(
            "SELECT execution_kind FROM reckoner.v1_protocol_executions WHERE protocol_sha256=%s",
            (protocol_id,),
        ).fetchone()["execution_kind"]
    if recorded != execution_kind:
        raise ValueError(f"this protocol was already executed as {recorded}")


def close_protocol(protocol_id: str, *, dsn: str) -> dict:
    """Release every envelope's unused capacity; uncertain calls keep their maximum."""
    with V1Repository(dsn) as repo:
        protocol = load_recorded(repo, protocol_id)
        ledger = ProviderBudget(repo._connection)
        for dispatch in protocol["dispatch"]:
            ledger.close(dispatch["protocol_id"])
        return {"protocol_sha256": protocol_id, "closed": len(protocol["dispatch"])}


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
        if protocol["telemetry_mode"] == "workflow" and protocol["provider"] == "typesafe":
            if data_kind not in {"fabricated", "simulated-cctd"}:
                raise ValueError("workflow execution needs an explicit data kind")
        directory = _output(output_dir)  # refuse an existing directory before recording
        _record_kind(repo, protocol_id, execution_kind)
        if protocol["provider"] == "anthropic":
            results, stop, error = _note_cases(repo, protocol, client)
        elif protocol["telemetry_mode"] == "workflow":
            results, stop, error = _workflow_cases(
                repo, protocol, client, dsn, {"data_kind": data_kind, "calibration": calibration}
            )
        else:
            results, stop, error = _score_cases(repo, protocol, client)
        terminal = {"responded", "failed", "uncertain", "decided", "succeeded", "invalid"}
        own = [r for r in results if r["status"] != "evaluator-owned"]
        resumable = any(r["status"] not in terminal | {"not-dispatched"} for r in own)
        # An error leaves envelopes open (state explicit and resumable after repair);
        # a budget stop or terminal results close the executed (non-judge) envelopes.
        closed = error is None and (stop is not None or not resumable)
        ledger = ProviderBudget(repo._connection)
        if closed:
            # Judge envelopes stay open for the evaluator; close them with close_protocol.
            for dispatch in protocol["dispatch"]:
                if dispatch["purpose"] != "judge":
                    ledger.close(dispatch["protocol_id"])
        retained = retain_outputs(repo, protocol, directory)  # before telemetry can fail
        if error is not None:
            telemetry = {"collected": False, "reason": "execution error"}
        else:
            try:
                telemetry = _telemetry(repo, protocol, closed)
            except Exception as exc:  # recorded in the summary; outputs already retained
                telemetry = {"collected": False, "error": f"{type(exc).__name__}: {exc}"}
        totals = repo._connection.execute(
            "SELECT COALESCE(sum(s.cost),0) AS settled, count(*) FILTER (WHERE s.cost IS NULL) "
            "AS uncertain FROM reckoner.v1_provider_calls c LEFT JOIN reckoner.v1_settlements s "
            "ON s.call_id=c.call_id AND s.status='settled' WHERE c.protocol_id = ANY(%s)",
            ([d["protocol_id"] for d in protocol["dispatch"]],),
        ).fetchone()
        dispatched = (
            stop is None
            and error is None
            and all(r["status"] in {"responded", "decided", "succeeded"} for r in own)
            and totals["uncertain"] == 0
        )
        judges_pending = any(r["status"] == "evaluator-owned" for r in results)
        if error is not None:
            status = "error"
        elif stop:
            status = "stopped"
        elif not dispatched:
            status = "incomplete"
        else:
            status = "dispatched; judges pending" if judges_pending else "complete"
        summary = {
            "protocol_sha256": protocol_id,
            "provider": protocol["provider"],
            "purpose": protocol["purpose"],
            "execution_kind": execution_kind,
            "status": status,
            "stop_reason": stop,
            "error": None if error is None else f"{type(error).__name__}: {error}",
            "closed": closed and not judges_pending,
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
        if error is not None:
            raise error
        return summary

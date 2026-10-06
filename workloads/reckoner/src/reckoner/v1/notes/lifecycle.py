"""Late note work leaves root decisions and captured reviewer recommendations intact."""

from reckoner.v1.notes.calls import BudgetedCalls
from reckoner.v1.notes.generate import generate_note
from reckoner.v1.notes.prompt import build_note_request


def declare_note(repo, decision):
    if decision["outcome"] != "escalate":
        return
    identity = {key: decision[key] for key in ("tenant_id", "run_id", "task_id")}
    doc = {
        **identity,
        "case_id": decision["decision_id"],
        "decision_id": decision["decision_id"],
        "evidence_id": decision["evidence_id"],
        "status": "pending",
        "declared_at": decision["completed_at"],
    }
    repo._insert(
        "v1_note_work",
        {**identity, "case_id": doc["case_id"], "document": doc},
        {key: doc[key] for key in ("tenant_id", "case_id")},
    )


def note_work(repo, decision):
    identity = {"tenant_id": decision["tenant_id"], "case_id": decision["decision_id"]}
    return repo.workflow_document("v1_note_results", identity) or repo.workflow_document(
        "v1_note_work", identity
    )


def continue_note(repo, decision):
    if decision["outcome"] != "escalate":
        return
    declare_note(repo, decision)
    if note_work(repo, decision)["status"] != "pending":
        return
    protocol = getattr(repo, "note_protocol", None)
    client = getattr(repo, "note_client", None)
    if protocol is None or client is None:
        return
    task = repo.task(*(decision[k] for k in ("tenant_id", "run_id", "task_id")))
    config = repo.workflow_document(
        "v1_configs", {"tenant_id": task["tenant_id"], "config_id": task["config_id"]}
    )
    evidence = repo.workflow_document(
        "v1_evidence", {"tenant_id": task["tenant_id"], "evidence_id": decision["evidence_id"]}
    )
    row = repo._connection.execute(
        "SELECT score FROM reckoner.v1_provider_responses WHERE tenant_id=%s AND call_id=%s",
        (task["tenant_id"], decision["call_id"]),
    ).fetchone()
    score = row["score"] if row else {}
    calls = BudgetedCalls(repo, client, task, config, kind="note")
    # Preflight before any stage attachment; bad approvals cannot poison a case.
    calls.preflight(
        build_note_request(decision, evidence, score, config), "note-generation", protocol
    )
    result = generate_note(decision, evidence, score, calls, config, protocol=protocol)
    identity = {"tenant_id": task["tenant_id"], "case_id": decision["decision_id"]}
    with repo._connection.transaction():
        case = repo._connection.execute(
            "SELECT reckoner.v1_lock_note_case(%s,%s) AS status", tuple(identity.values())
        ).fetchone()
        result = {**identity, **result, "available_before_review": case["status"] == "pending"}
        if result["note"] is not None and result["available_before_review"]:
            note = result["note"]
            values = {
                key: note[key]
                for key in (
                    "tenant_id",
                    "note_id",
                    "case_id",
                    "decision_id",
                    "evidence_id",
                    "generation_status",
                    "prompt_version",
                    "completed_at",
                )
            }
            repo._insert(
                "v1_notes",
                {**values, "document": note},
                {key: note[key] for key in ("tenant_id", "note_id")},
            )
        repo._insert("v1_note_results", {**identity, "document": result}, identity)

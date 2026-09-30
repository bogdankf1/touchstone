"""Immutable run policy and task references, with one lifecycle owner per task."""

from contextlib import contextmanager
from datetime import UTC, datetime

from reckoner.contracts import content_id
from reckoner.v1.calibration import validate_calibration_context
from reckoner.v1.contracts import validate_v1
from reckoner.v1.storage.attempts import _preflight
from reckoner.v1.storage.budget import validate_protocol
from reckoner.v1.storage.checkpoints import task_config

IDENTITY = ("tenant_id", "run_id", "task_id")
SETTINGS = {"evidence_id", "evidence_mode", "data_kind", "protocol", "calibration"}


def calibration_context(config, policy):
    return {
        key: config[key]
        for key in ("scorer", "feature_version", "scaler_id", "graph_version", "retrieval_version")
    } | {key: policy[key] for key in ("evidence_mode", "data_kind")}


def configuration(repo, task):
    document = repo.workflow_document(
        "v1_configs", {"tenant_id": task["tenant_id"], "config_id": task["config_id"]}
    )
    return validate_v1("run-config", document)


@contextmanager
def execution_lock(repo, identity):
    # Different namespace from the nested Task4 scorer lock; opaque IDs stay opaque.
    lock = int(content_id(["workflow", *identity.values()])[:15], 16)
    repo._connection.execute("SELECT pg_advisory_lock(%s)", (lock,))
    try:
        yield
    finally:
        repo._connection.execute("SELECT pg_advisory_unlock(%s)", (lock,))


def _bind(repo, task, settings):
    if set(settings) != SETTINGS:
        raise ValueError("explicit workflow settings required")
    if settings["evidence_mode"] not in {"relational", "gds-augmented"}:
        raise ValueError("unknown evidence mode")
    if settings["data_kind"] not in {"fabricated", "simulated-cctd"}:
        raise ValueError("explicit simulated/fabricated data kind required")
    if settings["protocol"] is not None:
        validate_protocol(settings["protocol"])
    config = configuration(repo, task)
    run_identity = {key: task[key] for key in ("tenant_id", "run_id")}
    policy = {
        **run_identity,
        "config_id": task["config_id"],
        **{key: settings[key] for key in ("evidence_mode", "data_kind", "calibration")},
    }
    artifact = settings["calibration"]
    if config["score_mode"] == "calibrated":
        if artifact is None or artifact.get("calibration_id") != config["calibration_id"]:
            raise ValueError("calibration identity does not match pinned configuration")
        validate_calibration_context(artifact, calibration_context(config, policy))
    elif artifact is not None or config["calibration_id"] is not None:
        raise ValueError("raw score mode requires no calibration")
    evidence = repo.workflow_document(
        "v1_evidence", {"tenant_id": task["tenant_id"], "evidence_id": settings["evidence_id"]}
    )
    if evidence is None:
        raise ValueError("persist a bounded unavailable evidence bundle before execution")
    if settings["evidence_mode"] == "relational" and (
        evidence.get("graph_projection") or evidence["source_snapshot_ids"]["graph"] is not None
    ):
        raise ValueError("evidence mode does not match graph treatment")
    if evidence["transaction_id"] != task["transaction_id"]:
        raise ValueError("evidence transaction mismatch")
    if datetime.fromisoformat(evidence["query_time"]) != datetime.fromisoformat(
        task["transaction"]["occurred_at"]
    ):
        raise ValueError("evidence query time mismatch")
    if evidence["coverage"]["status"] == "available" and (
        settings["evidence_mode"] == "relational" or evidence.get("graph_projection")
    ):
        if settings["protocol"] is None:
            raise ValueError("available evidence requires an explicit approved protocol")
        # Share the scorer's read-only checks before pinning irreversible inputs.
        _preflight(repo, task, evidence, settings["protocol"])
    identity = {key: task[key] for key in IDENTITY}
    previous = repo.workflow_document("v1_workflow_tasks", identity)
    binding = {
        **identity,
        "transaction_id": task["transaction_id"],
        "config_id": task["config_id"],
        "evidence_id": settings["evidence_id"],
        "protocol": settings["protocol"],
        "protocol_id": settings["protocol"]["protocol_id"] if settings["protocol"] else None,
        "calibration_id": config["calibration_id"],
        "started_at": previous["started_at"] if previous else datetime.now(UTC).isoformat(),
    }
    with repo._connection.transaction():
        repo._insert(
            "v1_workflow_runs",
            {**run_identity, "config_id": task["config_id"], "document": policy},
            run_identity,
        )
        repo._insert(
            "v1_workflow_tasks",
            {
                **{key: binding[key] for key in (*IDENTITY, "transaction_id", "evidence_id")},
                "document": binding,
            },
            identity,
        )
    return binding


def _execute(repo, graph, task, binding):
    config = task_config(task)
    snapshot = graph.get_state(config)
    if snapshot.values and not snapshot.next:
        return repo.workflow_document("v1_decisions", {key: task[key] for key in IDENTITY})
    initial = {
        key: binding[key]
        for key in (*IDENTITY, "transaction_id", "config_id", "evidence_id", "started_at")
    }
    initial.update(
        checkpoint_version=1,
        status="running",
        protocol_id=binding["protocol_id"],
        calibration_id=binding["calibration_id"],
    )
    graph.invoke(
        None if snapshot.values else initial, config, context={"repo": repo}, durability="sync"
    )
    decision = repo.workflow_document("v1_decisions", {key: task[key] for key in IDENTITY})
    if decision is None:
        raise RuntimeError("workflow ended without persisted decision")
    return decision


def run_task(repo, graph, task: dict, config: dict) -> dict:
    identity = {key: task[key] for key in IDENTITY}
    with execution_lock(repo, identity):
        actual = repo.task(**identity)
        if any(
            task.get(key, actual[key]) != actual[key] for key in ("transaction_id", "config_id")
        ):
            raise ValueError("task identity/configuration mismatch")
        binding = _bind(repo, actual, config)
        decision = _execute(repo, graph, actual, binding)
        from reckoner.v1.notes.lifecycle import continue_note

        continue_note(repo, decision)
        return decision


def resume_task(repo, graph, tenant_id: str, run_id: str, task_id: str) -> dict:
    identity = dict(zip(IDENTITY, (tenant_id, run_id, task_id), strict=True))
    with execution_lock(repo, identity):
        actual = repo.task(**identity)
        binding = repo.workflow_document("v1_workflow_tasks", identity)
        if binding is None:
            raise LookupError("workflow task has not started")
        decision = _execute(repo, graph, actual, binding)
        from reckoner.v1.notes.lifecycle import continue_note

        continue_note(repo, decision)
        return decision

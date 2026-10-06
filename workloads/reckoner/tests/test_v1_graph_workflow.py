"""Durable routing uses real persistence and fabricated injected HTTP only."""

import importlib
import json
from copy import deepcopy
from decimal import Decimal

import httpx
import psycopg
import pytest
from reckoner.v1.storage.repository import V1Repository
from test_v1_budget import scoring
from test_v1_jev import response
from v1_fixtures import identified

pytestmark = pytest.mark.integration


def workflow():
    try:
        return importlib.import_module("reckoner.v1.workflow"), importlib.import_module(
            "reckoner.v1.storage.checkpoints"
        )
    except ModuleNotFoundError:
        pytest.fail("durable LangGraph workflow is not implemented")


def prepared(pg, probability="0.2", status=200, **options):
    repo, task, evidence, protocol, _, jev = scoring(pg, attempts=1, **options)
    requests = []

    def handle(request):
        requests.append(request)
        body = response()
        body["answers"]["risk"]["probabilities"] = {
            "fraud": probability,
            "legitimate": str(1 - Decimal(probability)),
        }
        body["answers"]["risk"]["choice"] = (
            "fraud" if Decimal(probability) > Decimal(".5") else "legitimate"
        )
        return httpx.Response(status, json=body)

    repo.scorer_client = jev.JevClient("fabricated-only", transport=httpx.MockTransport(handle))
    repo._test_checkpoint_dsn = pg.runner_dsn
    repo.persist_evidence(evidence)
    settings = {
        "evidence_id": evidence["evidence_id"],
        "evidence_mode": "relational",
        "data_kind": "fabricated",
        "protocol": protocol,
        "calibration": None,
    }
    return repo, task, evidence, settings, requests


def execute(repo, task, settings, saver_class=None):
    api, checkpoints = workflow()
    with (saver_class or checkpoints.PostgresCheckpointer)(
        repo._test_checkpoint_dsn, task["tenant_id"]
    ) as saver:
        graph = api.build_graph(saver)
        result = api.run_task(repo, graph, task, settings)
        return result, graph.get_state(checkpoints.task_config(task))


@pytest.mark.parametrize(
    "probability,outcome",
    [("0.001", "auto-approve"), ("0.95", "auto-decline"), ("0.2", "escalate")],
)
def test_decision_terminal_persistence_and_repeated_execution(pg, probability, outcome):
    repo, task, _, settings, calls = prepared(pg, probability)
    with repo:
        decision, snapshot = execute(repo, task, settings)
        assert decision["outcome"] == outcome
        assert not snapshot.next
        assert snapshot.values["decision_id"] == decision["decision_id"]
        assert snapshot.values["status"] == "completed"
        assert execute(repo, task, settings)[0] == decision
        assert len(calls) == 1
        assert (
            repo.task(task["tenant_id"], task["run_id"], task["task_id"])["status"] == "completed"
        )
    with psycopg.connect(pg.owner_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM reckoner.v1_decisions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM reckoner.v1_cases").fetchone()[0] == (
            outcome == "escalate"
        )


def test_unavailable_evidence_creates_reviewable_case_without_dispatch(pg):
    repo, task, evidence, settings, calls = prepared(pg)
    evidence["coverage"] = {"status": "unavailable", "missing": ["history"]}
    identified(evidence, "evidence_id")
    with repo:
        repo.persist_evidence(evidence)
        settings["evidence_id"] = evidence["evidence_id"]
        decision, snapshot = execute(repo, task, settings)
        assert decision["outcome"] == "escalate"
        assert decision["scorer_status"] == "unavailable"
        assert decision["degraded_reason"] == "evidence_unavailable"
        assert snapshot.values["evidence_status"] == "unavailable"
        assert calls == []
    with psycopg.connect(pg.api_dsn) as api:
        assert api.execute("SELECT status FROM reckoner.api_v1_cases").fetchone()[0] == "pending"
        assert api.execute("SELECT count(*) FROM reckoner.api_v1_notes").fetchone()[0] == 0


def test_provider_failure_is_degraded_not_an_automatic_decision(pg):
    repo, task, _, settings, calls = prepared(pg, status=401)
    with repo:
        decision, _ = execute(repo, task, settings)
    assert decision["outcome"] == "escalate"
    assert decision["scorer_status"] == "failed"
    assert decision["effective_probability"] is None
    assert len(calls) == 1


def test_checkpoint_state_has_only_bounded_references_and_runtime_is_not_serialized(pg):
    repo, task, _, settings, _ = prepared(pg)
    task["oracle_label"] = "DO-NOT-CHECKPOINT"
    task["unrestricted_history"] = "DO-NOT-CHECKPOINT"
    with repo:
        _, snapshot = execute(repo, task, settings)
    from reckoner.v1.workflow.state import WorkflowState

    assert set(snapshot.values) <= set(WorkflowState.__annotations__)
    serialized = json.dumps(snapshot.values)
    for forbidden in (
        "DO-NOT-CHECKPOINT",
        "fabricated-only",
        "oracle",
        'transaction"',
        'protocol"',
    ):
        assert forbidden not in serialized
    _, checkpoints = workflow()
    with checkpoints.PostgresCheckpointer(pg.runner_dsn, "tenant-a") as saver:
        history = list(saver.list(checkpoints.task_config(task)))
        assert len(history) >= 5
        for item in history:
            assert "fabricated-only" not in repr(item)
            assert "DO-NOT-CHECKPOINT" not in repr(item)
        wrong = checkpoints.task_config({**task, "tenant_id": "tenant-b"})
        with pytest.raises(ValueError, match="tenant"):
            saver.get_tuple(wrong)
    with psycopg.connect(pg.api_dsn) as api:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api.execute("SELECT * FROM reckoner.v1_checkpoints")


def test_task_and_run_policy_are_immutable_even_after_completion(pg):
    repo, task, evidence, settings, _ = prepared(pg)
    with repo:
        first, _ = execute(repo, task, settings)
        for key, value in (("data_kind", "simulated-cctd"), ("evidence_mode", "gds-augmented")):
            changed = {**settings, key: value}
            with pytest.raises(ValueError, match="conflict"):
                execute(repo, task, changed)
        altered = deepcopy(evidence)
        altered["coverage"] = {"status": "partial", "missing": ["graph"]}
        identified(altered, "evidence_id")
        repo.persist_evidence(altered)
        with pytest.raises(ValueError, match="conflict"):
            execute(repo, task, {**settings, "evidence_id": altered["evidence_id"]})
        assert execute(repo, task, settings)[0] == first


def test_missing_mandatory_graph_evidence_cannot_silently_use_relational_arm(pg):
    repo, task, _, settings, calls = prepared(pg)
    settings["evidence_mode"] = "gds-augmented"
    with repo:
        decision, state = execute(repo, task, settings)
        assert decision["outcome"] == "escalate"
        assert decision["degraded_reason"] == "evidence_unavailable"
        assert state.values["evidence_status"] == "unavailable"
        assert calls == []


def test_protocol_with_secret_extra_field_rejected_before_persistence(pg):
    repo, task, _, settings, calls = prepared(pg)
    settings["protocol"]["api_key"] = "DO-NOT-PERSIST"
    with repo:
        with pytest.raises(ValueError):
            execute(repo, task, settings)
        assert (
            repo.workflow_document(
                "v1_workflow_tasks", {k: task[k] for k in ("tenant_id", "run_id", "task_id")}
            )
            is None
        )
        assert calls == []


def calibrated_setup(pg):
    from reckoner.contracts import content_id
    from reckoner.v1.calibration import fit_calibration
    from reckoner.v1.providers.jev import build_request
    from test_v1_calibration import rehash, sample
    from v1_fixtures import experiment_fixture

    repo, task, evidence, settings, calls = prepared(pg)
    config = repo.workflow_document(
        "v1_configs", {"tenant_id": task["tenant_id"], "config_id": task["config_id"]}
    )
    config["scaler_id"] = "a" * 64
    rows = sample()
    for row in rows:
        row["context"]["scorer"]["question_version"] = "binary-v1"
    artifact = fit_calibration(rows)
    artifact["qualification"] = {
        "status": "selected",
        "validation_id": "b" * 64,
        "raw_brier": "0.04",
        "candidate_brier": "0.001",
        "raw_log_loss": "0.2",
        "candidate_log_loss": "0.02",
    }
    rehash(artifact)
    config.update(score_mode="calibrated", calibration_id=artifact["calibration_id"])
    identified(config, "config_id")
    manifest = experiment_fixture(config, task["transaction_id"])
    manifest["run_id"] = "calibrated-run"
    identified(manifest, "experiment_id")
    with V1Repository(pg.owner_dsn) as owner:
        owner.register_config(config)
        owner.create_run(manifest, config["config_id"])
    task = repo.task("tenant-a", manifest["run_id"], "task-a")
    previous = settings["protocol"]["protocol_id"]
    settings["protocol"]["run_id"] = task["run_id"]
    settings["protocol"]["tasks"] = [
        {
            "task_id": task["task_id"],
            "transaction_id": task["transaction_id"],
            "request_sha256": content_id(build_request(task["transaction"], evidence)),
        }
    ]
    identified(settings["protocol"], "protocol_id")
    from v1_fixtures import reauthorize

    reauthorize(pg.owner_dsn, previous, settings["protocol"])
    settings["calibration"] = artifact
    return repo, task, settings, calls


def test_qualified_calibration_drives_decision_instead_of_raw_probability(pg):
    from reckoner.v1.calibration import apply_calibration

    repo, task, settings, calls = calibrated_setup(pg)
    with repo:
        decision, state = execute(repo, task, settings)
        effective = apply_calibration(Decimal(".2"), settings["calibration"])
        assert Decimal(decision["raw_probability"]) == Decimal(".2")
        assert Decimal(decision["effective_probability"]) == effective
        assert effective < Decimal(decision["effective_low_threshold"])
        assert decision["outcome"] == "auto-approve"
        assert state.values["calibration_id"] == settings["calibration"]["calibration_id"]
        assert len(calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("evidence_mode", "gds-augmented"),
        ("data_kind", "simulated-cctd"),
        ("calibration_id", "c" * 64),
    ],
)
def test_calibration_must_match_independently_pinned_policy_and_identity(pg, field, value):
    repo, task, settings, calls = calibrated_setup(pg)
    if field == "calibration_id":
        settings["calibration"][field] = value
    else:
        settings[field] = value
    with repo:
        with pytest.raises(ValueError, match="context|identity"):
            execute(repo, task, settings)
        assert calls == []


def test_gds_nonconvergence_is_explicit_and_retains_evidence_reference(pg):
    from reckoner.contracts import content_id
    from reckoner.v1.evidence.neo4j import PARAMETERS
    from reckoner.v1.providers.jev import build_request

    repo, task, evidence, settings, calls = prepared(pg)
    cutoff = task["transaction"]["occurred_at"]
    projection = {
        "projection_id": "a" * 64,
        "cutoff": cutoff,
        "window_days": 30,
        "gds_version": "fabricated-gds",
        "algorithm": "louvain-page-rank-v1",
        "parameters": PARAMETERS,
        "node_count": 2,
        "edge_count": 1,
        "covered_accounts": 1,
        "covered_cards": 1,
        "covered_merchants": 1,
        "build_seconds": 0.1,
        "page_rank_converged": False,
        "page_rank_iterations": 20,
        "community_count": 1,
        "snapshot_age_seconds": "0",
        "cross_tenant": False,
    }
    projection["projection_id"] = content_id(
        {k: v for k, v in projection.items() if k not in {"projection_id", "snapshot_age_seconds"}}
    )
    evidence["graph_projection"] = projection
    evidence["source_snapshot_ids"]["graph"] = projection["projection_id"]
    evidence["cutoffs"]["graph_before"] = cutoff
    evidence["coverage"] = {"status": "partial", "missing": ["PageRank nonconvergence"]}
    identified(evidence, "evidence_id")
    settings.update(evidence_id=evidence["evidence_id"], evidence_mode="gds-augmented")
    settings["protocol"]["tasks"][0]["request_sha256"] = content_id(
        build_request(task["transaction"], evidence)
    )
    identified(settings["protocol"], "protocol_id")
    with repo:
        repo.persist_evidence(evidence)
        decision, state = execute(repo, task, settings)
        assert state.values["pagerank_status"] == "nonconverged"
        assert decision["evidence_id"] == evidence["evidence_id"]
        assert decision["scorer_status"] == "unavailable"
        assert calls == []


def test_relational_policy_rejects_graph_treatment_instead_of_relabelling(pg):
    repo, task, evidence, settings, calls = prepared(pg)
    # Even an unavailable graph reference cannot qualify as the relational treatment.
    evidence["source_snapshot_ids"]["graph"] = "a" * 64
    identified(evidence, "evidence_id")
    with repo:
        repo.persist_evidence(evidence)
        settings["evidence_id"] = evidence["evidence_id"]
        with pytest.raises(ValueError, match="evidence mode"):
            execute(repo, task, settings)
        assert calls == []


def test_missing_protocol_for_available_evidence_is_rejected_before_binding(pg):
    repo, task, _, settings, calls = prepared(pg)
    settings["protocol"] = None
    with repo:
        with pytest.raises(ValueError, match="protocol"):
            execute(repo, task, settings)
        assert (
            repo.workflow_document(
                "v1_workflow_tasks", {k: task[k] for k in ("tenant_id", "run_id", "task_id")}
            )
            is None
        )
        assert calls == []


def test_unavailable_evidence_does_not_require_a_paid_protocol_or_client(pg):
    repo, task, evidence, settings, calls = prepared(pg)
    settings["protocol"] = None
    repo.scorer_client = None
    evidence["coverage"] = {"status": "unavailable", "missing": ["history"]}
    identified(evidence, "evidence_id")
    settings["evidence_id"] = evidence["evidence_id"]
    with repo:
        repo.persist_evidence(evidence)
        decision, _ = execute(repo, task, settings)
        assert decision["outcome"] == "escalate"
        assert decision["call_id"] is None
        assert calls == []


@pytest.mark.parametrize(
    "mismatch",
    [
        "tenant",
        "run",
        "task",
        "transaction",
        "request_hash",
        "scorer",
        "attempt_limit",
        "input_limit",
        "output_limit",
    ],
)
def test_invalid_protocol_preflight_leaves_task_unbound_for_corrected_retry(pg, mismatch):
    from reckoner.v1.storage.budget import validate_protocol

    repo, task, _, settings, calls = prepared(pg)
    invalid = deepcopy(settings)
    protocol = invalid["protocol"]
    if mismatch in {"tenant", "run"}:
        protocol[mismatch + "_id"] = "different-" + mismatch
    elif mismatch in {"task", "transaction"}:
        protocol["tasks"][0][mismatch + "_id"] = "different-" + mismatch
    elif mismatch == "request_hash":
        protocol["tasks"][0]["request_sha256"] = "f" * 64
    elif mismatch == "scorer":
        # Structurally supported, but incompatible with this run's pinned Jev scorer.
        protocol.update(provider="anthropic", model="anthropic/claude-haiku-4-5-20251001")
    else:
        key = {
            "attempt_limit": "maximum_attempts",
            "input_limit": "input_token_ceiling",
            "output_limit": "max_output_tokens",
        }[mismatch]
        protocol[key] += 1
    identified(protocol, "protocol_id")
    validate_protocol(protocol)
    identity = {k: task[k] for k in ("tenant_id", "run_id", "task_id")}
    with repo:
        with pytest.raises(ValueError, match="protocol"):
            execute(repo, task, invalid)
        assert (
            repo.workflow_document(
                "v1_workflow_runs", {"tenant_id": task["tenant_id"], "run_id": task["run_id"]}
            )
            is None
        )
        assert repo.workflow_document("v1_workflow_tasks", identity) is None
        assert repo.workflow_document("v1_decisions", identity) is None
        assert (
            repo._connection.execute(
                "SELECT count(*) AS n FROM reckoner.v1_provider_calls"
            ).fetchone()["n"]
            == 0
        )
        assert calls == []
        decision, snapshot = execute(repo, task, settings)
        assert decision["scorer_status"] == "succeeded"
        assert snapshot.values["protocol_id"] == settings["protocol"]["protocol_id"]
        assert len(calls) == 1

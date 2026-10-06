"""Additive v1 storage preserves baseline data and tenant/role boundaries."""

import importlib
import shutil
from copy import deepcopy

import psycopg
import pytest
from conftest import CONFIG_DIR, seed_run
from psycopg.types.json import Jsonb
from reckoner.storage.migrate import migrate
from reckoner.storage.postgres import PostgresRepository
from v1_fixtures import (
    config_fixture,
    decision_fixture,
    evidence_fixture,
    experiment_fixture,
    identified,
)

pytestmark = pytest.mark.integration


def repository(dsn):
    try:
        module = importlib.import_module("reckoner.v1.storage.repository")
    except ModuleNotFoundError:
        pytest.fail("v1 repository is not implemented")
    return module.V1Repository(dsn)


def setup_run(pg):
    task = seed_run(pg, "baseline-preserved", "tenant-a", purpose="pilot")
    import json

    threshold = json.loads((CONFIG_DIR / "thresholds-tenant-a-v1.json").read_text())
    config = config_fixture(threshold=threshold["config_id"])
    manifest = experiment_fixture(config, task["transaction"]["transaction_id"])
    with repository(pg.owner_dsn) as repo:
        assert repo.register_config(config) == config["config_id"]
        assert repo.register_config(config) == config["config_id"]
        assert repo.create_run(manifest, config["config_id"]) == manifest
        assert repo.create_run(manifest, config["config_id"]) == manifest
    return config, manifest, task


def test_migration_adds_v1_without_changing_baseline_and_rejects_changed_bytes(
    pg, tmp_path, monkeypatch
):
    config, manifest, baseline_task = setup_run(pg)
    migrate(pg.owner_dsn)
    with PostgresRepository(pg.runner_dsn) as baseline:
        assert any(
            task["task_id"] == baseline_task["task_id"]
            for task in baseline.pending_tasks("baseline-preserved")
        )
    with repository(pg.runner_dsn) as repo:
        task = repo.task("tenant-a", manifest["run_id"], "task-a")
        assert task["transaction"] == baseline_task["transaction"]
        assert task["status"] == "pending"
        assert task["config_id"] == config["config_id"]
        with pytest.raises(LookupError):
            repo.task("tenant-b", manifest["run_id"], "task-a")
    module = importlib.import_module("reckoner.storage.migrate")
    copied = tmp_path / "migrations"
    shutil.copytree(module.MIGRATIONS, copied)
    path = copied / "005_v1_records.sql"
    path.write_text(path.read_text() + "\n-- altered\n")
    monkeypatch.setattr(module, "MIGRATIONS", copied)
    with pytest.raises(ValueError, match="checksum"):
        migrate(pg.owner_dsn)


def test_degraded_decision_persistence_is_atomic_idempotent_and_conflicts_fail(pg):
    config, manifest, task = setup_run(pg)
    evidence = evidence_fixture(transaction_id=task["transaction"]["transaction_id"])
    decision = decision_fixture(config, evidence, degraded=True)
    with repository(pg.runner_dsn) as repo:
        assert repo.persist_evidence(evidence) == evidence["evidence_id"]
        assert repo.persist_evidence(evidence) == evidence["evidence_id"]
        assert repo.persist_decision(decision) == decision
        assert repo.persist_decision(decision) == decision
        assert repo.task("tenant-a", manifest["run_id"], "task-a")["status"] == "completed"
        conflict = deepcopy(decision)
        conflict["degraded_reason"] = "different reason"
        identified(conflict, "decision_id")
        with pytest.raises(ValueError, match="conflict"):
            repo.persist_decision(conflict)
        assert repo.task("tenant-a", manifest["run_id"], "task-a")["status"] == "completed"
    with psycopg.connect(pg.owner_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM reckoner.v1_decisions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM reckoner.v1_cases").fetchone()[0] == 1


def test_foreign_tenant_and_wrong_pinned_config_or_evidence_references_fail(pg):
    config, manifest, task = setup_run(pg)
    evidence = evidence_fixture(transaction_id=task["transaction"]["transaction_id"])
    with repository(pg.runner_dsn) as repo:
        repo.persist_evidence(evidence)
        crossed = decision_fixture(config, evidence, degraded=True)
        crossed["tenant_id"] = "tenant-b"
        identified(crossed, "decision_id")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            repo.persist_decision(crossed)
        wrong_config = config_fixture(threshold=config["threshold_config_id"])
        wrong_config["limits"]["timeout_seconds"] = 45
        identified(wrong_config, "config_id")
        with repository(pg.owner_dsn) as owner:
            owner.register_config(wrong_config)
        mismatched = decision_fixture(wrong_config, evidence, degraded=True)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            repo.persist_decision(mismatched)
        wrong_evidence = evidence_fixture(transaction_id="missing-transaction")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            repo.persist_evidence(wrong_evidence)
        # A real second transaction still cannot become this task's evidence.
        with psycopg.connect(pg.owner_dsn) as connection:
            other = connection.execute(
                "SELECT transaction_id FROM reckoner.transactions WHERE tenant_id='tenant-a' "
                "AND transaction_id != %s LIMIT 1",
                (evidence["transaction_id"],),
            ).fetchone()[0]
        other_evidence = evidence_fixture(transaction_id=other)
        repo.persist_evidence(other_evidence)
        mismatched = decision_fixture(config, evidence, degraded=True)
        mismatched["evidence_id"] = other_evidence["evidence_id"]
        identified(mismatched, "decision_id")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            repo.persist_decision(mismatched)


def test_config_and_manifest_changes_conflict_and_failed_run_creation_rolls_back(pg):
    config, manifest, task = setup_run(pg)
    conflict = deepcopy(manifest)
    conflict["purpose"] = "validation"
    identified(conflict, "experiment_id")
    with repository(pg.owner_dsn) as repo:
        with pytest.raises(ValueError, match="conflict"):
            repo.create_run(conflict, config["config_id"])
        crossed = config_fixture(tenant="tenant-b", threshold=config["threshold_config_id"])
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            repo.register_config(crossed)
        invalid = deepcopy(manifest)
        invalid["run_id"] = "invalid-run"
        invalid["tasks"].append({"task_id": "missing-task", "transaction_id": "missing"})
        identified(invalid, "experiment_id")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            repo.create_run(invalid, config["config_id"])
        with pytest.raises(LookupError):
            repo.task("tenant-a", "invalid-run", "task-a")
    with psycopg.connect(pg.owner_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM reckoner.v1_runs").fetchone()[0] == 1


def test_runner_and_read_only_api_do_not_gain_oracle_or_immutable_write_privileges(pg):
    setup_run(pg)
    for dsn in (pg.runner_dsn, pg.api_dsn):
        with psycopg.connect(dsn) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM oracle.oracle_labels")
    with psycopg.connect(pg.api_dsn) as api:
        assert len(api.execute("SELECT * FROM reckoner.api_v1_runs").fetchall()) == 1
        for table in (
            "v1_configs",
            "v1_runs",
            "v1_tasks",
            "v1_evidence",
            "v1_decisions",
            "v1_cases",
            "v1_notes",
            "v1_reviews",
            "v1_outbox",
        ):
            assert not api.execute(
                "SELECT has_table_privilege(current_user, %s, 'SELECT')", (f"reckoner.{table}",)
            ).fetchone()[0]
            assert not api.execute(
                "SELECT has_table_privilege(current_user, %s, 'INSERT,UPDATE,DELETE')",
                (f"reckoner.{table}",),
            ).fetchone()[0]
        for view in (
            "api_v1_runs",
            "api_v1_tasks",
            "api_v1_decisions",
            "api_v1_cases",
            "api_v1_notes",
            "api_v1_reviews",
        ):
            assert not api.execute(
                "SELECT has_table_privilege(current_user, %s, 'INSERT,UPDATE,DELETE')",
                (f"reckoner.{view}",),
            ).fetchone()[0]
    with psycopg.connect(pg.runner_dsn) as runner:
        for table in ("v1_configs", "v1_evidence", "v1_decisions", "v1_notes", "v1_reviews"):
            assert not runner.execute(
                "SELECT has_table_privilege(current_user, %s, 'UPDATE,DELETE')",
                (f"reckoner.{table}",),
            ).fetchone()[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runner.execute("UPDATE reckoner.v1_configs SET document=%s", (Jsonb({}),))


def test_owner_cannot_rewrite_documents_or_indexed_identities(pg):
    config, manifest, task = setup_run(pg)
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute("UPDATE reckoner.v1_configs SET workflow_version='rewritten'")
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                "UPDATE reckoner.v1_configs SET document=document || %s",
                (Jsonb({"feature_version": "rewritten"}),),
            )
        owner.execute("UPDATE reckoner.v1_runs SET status='running'")


def test_note_evidence_must_match_the_case_decision_evidence(pg):
    from v1_fixtures import note_fixture

    config, manifest, task = setup_run(pg)
    evidence = evidence_fixture(transaction_id=task["transaction"]["transaction_id"])
    other = deepcopy(evidence)
    other["query_time"] = "2019-01-09T00:00:00Z"
    identified(other, "evidence_id")
    decision = decision_fixture(config, evidence, degraded=True)
    with repository(pg.runner_dsn) as repo:
        repo.persist_evidence(evidence)
        repo.persist_evidence(other)
        repo.persist_decision(decision)
    note = note_fixture()
    note.update(
        case_id=decision["decision_id"],
        decision_id=decision["decision_id"],
        evidence_id=other["evidence_id"],
    )
    identified(note, "note_id")
    with psycopg.connect(pg.owner_dsn) as owner:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            owner.execute(
                "INSERT INTO reckoner.v1_notes (tenant_id,note_id,case_id,decision_id,evidence_id,"
                "generation_status,prompt_version,document,completed_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    note["tenant_id"],
                    note["note_id"],
                    note["case_id"],
                    note["decision_id"],
                    note["evidence_id"],
                    note["generation_status"],
                    note["prompt_version"],
                    Jsonb(note),
                    note["completed_at"],
                ),
            )

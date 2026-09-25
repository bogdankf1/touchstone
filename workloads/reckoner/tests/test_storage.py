import importlib
import json
import shutil

import psycopg
import pytest
from conftest import CONFIG_DIR
from reckoner.baseline.config import load_config
from reckoner.cli import main
from reckoner.contracts import content_id
from reckoner.storage.budget import RunBusy
from reckoner.storage.migrate import migrate
from reckoner.storage.postgres import PostgresRepository

pytestmark = pytest.mark.integration


def _config():
    return load_config(CONFIG_DIR / "baseline-v1.json", CONFIG_DIR / "anthropic-prices-v1.json")


def _price():
    return json.loads((CONFIG_DIR / "anthropic-prices-v1.json").read_text())


def _register_thresholds(repo):
    for name in ("thresholds-tenant-a-v1.json", "thresholds-tenant-b-v1.json"):
        repo.register_threshold_config(json.loads((CONFIG_DIR / name).read_text()))


def test_migration_rerun_is_idempotent_and_changed_applied_sql_is_rejected(
    pg, tmp_path, monkeypatch
):
    migrate(pg.owner_dsn)
    module = importlib.import_module("reckoner.storage.migrate")
    copied = tmp_path / "migrations"
    shutil.copytree(module.MIGRATIONS, copied)
    (copied / "001_baseline.sql").write_text(
        (copied / "001_baseline.sql").read_text() + "\n-- changed after application\n"
    )
    monkeypatch.setattr(module, "MIGRATIONS", copied)

    with pytest.raises(ValueError, match="checksum"):
        migrate(pg.owner_dsn)


def test_import_and_create_run_are_idempotent_and_create_every_two_tenant_task(pg):
    bundle_id = json.loads((pg.bundle / "bundle.json").read_text())["bundle_id"]
    with PostgresRepository(pg.owner_dsn) as repo:
        repo.import_bundle(pg.bundle)
        repo.import_bundle(pg.bundle)
        _register_thresholds(repo)
        repo.create_run("run-two-tenant", "pilot", _config(), bundle_id, price=_price())
        repo.create_run("run-two-tenant", "pilot", _config(), bundle_id, price=_price())

    with PostgresRepository(pg.runner_dsn) as repo:
        tasks = repo.pending_tasks("run-two-tenant")
        snapshot = repo.snapshot("run-two-tenant", None)

    assert len(tasks) == 20
    assert {task["tenant_id"] for task in tasks} == {"tenant-a", "tenant-b"}
    assert all(
        set(task)
        == {
            "tenant_id",
            "run_id",
            "task_id",
            "transaction",
            "status",
            "request",
            "request_sha256",
            "input_token_estimate",
            "reservation_input_tokens",
            "reservation_cost",
            "trace_id",
            "span_id",
            "event_id",
        }
        for task in tasks
    )
    assert len(snapshot["runs"]) == 2
    assert len(snapshot["tasks"]) == 20


def test_create_run_rejects_missing_or_cross_tenant_threshold_reference_before_tasks(pg):
    bundle_id = json.loads((pg.bundle / "bundle.json").read_text())["bundle_id"]
    config = json.loads(json.dumps(_config()))
    with PostgresRepository(pg.owner_dsn) as repo:
        repo.import_bundle(pg.bundle)
        _register_thresholds(repo)
        missing = json.loads(json.dumps(config))
        missing["threshold_config_ids"].pop("tenant-b")
        missing["config_id"] = content_id(
            {key: value for key, value in missing.items() if key != "config_id"}
        )
        with pytest.raises(ValueError, match="tenant coverage"):
            repo.create_run("missing-threshold", "pilot", missing, bundle_id)
        crossed = json.loads(json.dumps(config))
        crossed["threshold_config_ids"]["tenant-a"] = crossed["threshold_config_ids"]["tenant-b"]
        crossed["config_id"] = content_id(
            {key: value for key, value in crossed.items() if key != "config_id"}
        )
        with pytest.raises(ValueError, match="threshold"):
            repo.create_run("crossed-threshold", "pilot", crossed, bundle_id)

    with psycopg.connect(pg.owner_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM reckoner.tasks").fetchone()[0] == 0


def test_create_run_rejects_reidentified_changes_to_fixed_execution_limits(pg):
    bundle_id = json.loads((pg.bundle / "bundle.json").read_text())["bundle_id"]
    with PostgresRepository(pg.owner_dsn) as repo:
        repo.import_bundle(pg.bundle)
        _register_thresholds(repo)
        for field, value in (
            ("temperature", 100),
            ("max_output_tokens", 1_000_000),
            ("timeout_seconds", 1),
            ("input_token_ceiling", 1),
        ):
            config = json.loads(json.dumps(_config()))
            config[field] = value
            config["config_id"] = content_id(
                {key: item for key, item in config.items() if key != "config_id"}
            )
            with pytest.raises(ValueError, match="run configuration"):
                repo.create_run(f"invalid-{field}", "pilot", config, bundle_id)


def test_role_privileges_enforce_oracle_and_sanitized_api_boundaries(pg):
    with PostgresRepository(pg.owner_dsn) as repo:
        repo.import_bundle(pg.bundle)

    with psycopg.connect(pg.runner_dsn) as runner:
        assert runner.execute("SELECT count(*) FROM reckoner.transactions").fetchone()[0] == 1020
        assert not runner.execute(
            "SELECT has_table_privilege(current_user, 'reckoner.decisions', 'UPDATE')"
        ).fetchone()[0]
        assert not runner.execute(
            "SELECT has_table_privilege(current_user, 'reckoner.telemetry_outbox', 'SELECT')"
        ).fetchone()[0]
        assert runner.execute(
            "SELECT has_table_privilege(current_user, 'reckoner.runner_telemetry_outbox', 'SELECT')"
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runner.execute("SELECT * FROM oracle.oracle_labels")
        runner.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runner.execute("SELECT * FROM reckoner.evaluations")

    with psycopg.connect(pg.evaluator_dsn) as evaluator:
        assert evaluator.execute("SELECT count(*) FROM oracle.oracle_labels").fetchone()[0] == 1020

    with psycopg.connect(pg.api_dsn) as api:
        api.execute("SELECT * FROM reckoner.api_runs").fetchall()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api.execute("SELECT * FROM reckoner.transactions")
        api.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api.execute("SELECT * FROM oracle.oracle_labels")


def test_cross_tenant_task_reference_is_rejected_by_database(pg):
    bundle_id = json.loads((pg.bundle / "bundle.json").read_text())["bundle_id"]
    with PostgresRepository(pg.owner_dsn) as repo:
        repo.import_bundle(pg.bundle)
        _register_thresholds(repo)
        repo.create_run("tenant-fk", "pilot", _config(), bundle_id, price=_price())
    with psycopg.connect(pg.runner_dsn) as connection:
        task = connection.execute(
            "SELECT tenant_id, task_id FROM reckoner.tasks "
            "WHERE run_id = 'tenant-fk' ORDER BY tenant_id LIMIT 1"
        ).fetchone()
        other_tenant = "tenant-b" if task[0] == "tenant-a" else "tenant-a"
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                "INSERT INTO reckoner.attempts "
                "(tenant_id, run_id, task_id, call_id, status, maximum_cost) "
                "VALUES (%s, 'tenant-fk', %s, 'wrong-tenant-call', 'dispatched', 0)",
                (other_tenant, task[1]),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                """
                INSERT INTO reckoner.runner_telemetry_outbox
                  (tenant_id, event_id, run_id, task_id, payload, producer)
                VALUES (%s, 'wrong-tenant-event', 'tenant-fk', %s, %s, 'runner')
                """,
                (other_tenant, task[1], b"payload"),
            )


def test_exclusive_runner_is_held_across_connections(pg):
    with PostgresRepository(pg.runner_dsn) as first, PostgresRepository(pg.runner_dsn) as second:
        with first.exclusive_runner():
            with pytest.raises(RunBusy):
                with second.exclusive_runner():
                    pass
        with second.exclusive_runner():
            pass


def test_cli_migrate_and_import_use_only_the_explicit_owner_environment(pg, tmp_path, capsys):
    env_file = tmp_path / "test.env"
    env_file.write_text(f"RECKONER_OWNER_DSN={pg.owner_dsn}\n")

    assert main(["migrate", "--env-file", str(env_file)]) == 0
    migrated = json.loads(capsys.readouterr().out)
    assert migrated == {"status": "migrated"}
    assert main(["import", "--env-file", str(env_file), "--artifact-dir", str(pg.bundle)]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert set(imported) == {"bundle_id"}


def test_cli_database_errors_never_echo_dsn_or_password(tmp_path, capsys):
    env_file = tmp_path / "bad.env"
    password = "do-not-print-this-password"
    env_file.write_text(f"RECKONER_OWNER_DSN=postgresql://nobody:{password}@127.0.0.1:1/missing\n")

    assert main(["migrate", "--env-file", str(env_file)]) == 2
    error = capsys.readouterr().err
    assert error == "migrate failed: database unavailable\n"
    assert password not in error

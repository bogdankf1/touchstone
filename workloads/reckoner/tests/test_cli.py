"""Exercise the installed wheel outside the checkout; never use a provider network."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_container_environment_is_explicit_and_allowlisted(monkeypatch):
    from reckoner.cli import _environment

    monkeypatch.setenv("RECKONER_RUNNER_DSN", "example-runner")
    monkeypatch.setenv("UNRELATED_SECRET", "canary")
    assert _environment(Path("-"))["RECKONER_RUNNER_DSN"] == "example-runner"
    assert "UNRELATED_SECRET" not in _environment(Path("-"))


@pytest.fixture(scope="session")
def installed_cli(tmp_path_factory):
    directory = tmp_path_factory.mktemp("installed-reckoner")
    wheel_dir = directory / "wheels"
    subprocess.run(
        [
            "uv",
            "build",
            "--offline",
            "--package",
            "reckoner",
            "--wheel",
            "--out-dir",
            str(wheel_dir),
        ],
        cwd=ROOT,
        check=True,
    )
    environment = directory / "venv"
    subprocess.run(
        ["uv", "venv", "--python", "3.12", str(environment)],
        check=True,
    )
    python = environment / "bin/python"
    # Sync consumes locked artifact URLs directly. An offline name/version install
    # also needs registry metadata, which a clean CI cache has never fetched.
    subprocess.run(
        [
            "uv",
            "sync",
            "--offline",
            "--package",
            "reckoner",
            "--frozen",
            "--no-dev",
            "--no-install-workspace",
        ],
        cwd=ROOT,
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(environment)},
        check=True,
    )
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            "--offline",
            "--no-deps",
            str(next(wheel_dir.glob("*.whl"))),
        ],
        check=True,
    )
    # A socket interceptor catches accidental SDK/provider HTTP even during imports.
    (directory / "sitecustomize.py").write_text(
        "import socket\nfrom pathlib import Path\n"
        "original = socket.socket.connect\n"
        "original_dns = socket.getaddrinfo\n"
        "def guarded_dns(host, *args, **kwargs):\n"
        "    if host not in ('127.0.0.1', '::1', 'localhost', None):\n"
        "        Path('forbidden-network').write_text('DNS attempted')\n"
        "        raise RuntimeError('external DNS is forbidden in CLI tests')\n"
        "    return original_dns(host, *args, **kwargs)\n"
        "socket.getaddrinfo = guarded_dns\n"
        "def guarded(self, address):\n"
        "    if isinstance(address, tuple) and address[0] not in ('127.0.0.1', '::1'):\n"
        "        Path('forbidden-network').write_text('attempted')\n"
        "        raise RuntimeError('network is forbidden in installed CLI tests')\n"
        "    return original(self, address)\n"
        "socket.socket.connect = guarded\n"
    )

    def invoke(*arguments, code=None):
        command = (
            [str(python), "-c", code] if code else [str(python), "-m", "reckoner.cli", *arguments]
        )
        result = subprocess.run(
            command,
            cwd=directory,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(directory)},
        )
        assert not (directory / "forbidden-network").exists()
        return result

    return invoke, directory


def test_installed_help_loads_without_checkout_or_network(installed_cli):
    invoke, _ = installed_cli
    result = invoke("--help")
    assert result.returncode == 0, result.stderr
    assert "smoke" in result.stdout


def test_installed_resources_and_provenance_are_self_contained(installed_cli):
    invoke, _ = installed_cli
    result = invoke(
        code="""
import json
from reckoner.cli import _run_documents
from reckoner.resources import CONFIG
from reckoner.baseline.prompt import build_request
from reckoner.baseline.runner import _code_revision
from reckoner.storage.migrate import MIGRATIONS
config, price, thresholds = _run_documents(CONFIG / 'baseline-v1.json')
assert len(thresholds) == 2
assert len(list(MIGRATIONS.glob('*.sql'))) >= 4
print(json.dumps({'revision': _code_revision(), 'model': config['model']}))
"""
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["model"] == "anthropic/claude-haiku-4-5-20251001"
    revision, digest = document["revision"].split(":")
    assert len(revision) == 40 and len(digest) == 64


@pytest.mark.parametrize("command", ["migrate", "smoke"])
def test_installed_missing_credentials_fail_safely(installed_cli, command):
    invoke, directory = installed_cli
    env = directory / "empty.env"
    env.write_text("RECKONER_OWNER_DSN=\n")
    arguments = [command, "--env-file", str(env)]
    if command == "smoke":
        arguments += ["--output", str(directory / "smoke")]
    result = invoke(*arguments)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_installed_invalid_path_does_not_disclose_path(installed_cli):
    invoke, directory = installed_cli
    result = invoke(
        "verify", "--source-dir", "missing", "--artifact-dir", str(directory / "private-canary")
    )
    assert result.returncode == 2
    assert "private-canary" not in result.stderr
    assert "Traceback" not in result.stderr


def test_installed_run_without_paid_flag_never_opens_credentials_or_network(installed_cli):
    invoke, _ = installed_cli
    result = invoke(
        "run",
        "--env-file",
        "missing-private-canary",
        "--artifact-dir",
        "missing",
        "--config",
        "missing",
        "--purpose",
        "pilot",
        "--run-id",
        "unapproved",
    )
    assert result.returncode == 2
    assert "--allow-paid" in result.stderr
    assert "private-canary" not in result.stderr


def test_installed_unknown_model_fails_before_network(installed_cli):
    invoke, _ = installed_cli
    result = invoke(
        code="""
import json, tempfile
from pathlib import Path
from reckoner.resources import CONFIG
from reckoner.contracts import content_id
from reckoner.baseline.config import load_config
config = json.loads((CONFIG / 'baseline-v1.json').read_text())
config['model'] = 'anthropic/unknown-model'
config['config_id'] = content_id({k:v for k,v in config.items() if k != 'config_id'})
path = Path('unknown.json')
path.write_text(json.dumps(config))
try:
    load_config(path, CONFIG / 'anthropic-prices-v1.json')
except ValueError:
    raise SystemExit(2)
"""
    )
    assert result.returncode == 2, result.stderr


@pytest.mark.integration
@pytest.mark.parametrize("pg", ["smoke"], indirect=True)
def test_installed_smoke_exports_fake_evidence_and_resumes_without_new_calls(installed_cli, pg):
    import psycopg
    from reckoner.contracts import validate_cohort_manifest
    from reckoner.resources import SCHEMAS

    invoke, directory = installed_cli
    env = directory / "smoke.env"
    env.write_text(
        f"RECKONER_OWNER_DSN={pg.owner_dsn}\nRECKONER_RUNNER_DSN={pg.runner_dsn}\n"
        f"RECKONER_EVALUATOR_DSN={pg.evaluator_dsn}\n"
    )
    env.chmod(0o600)
    for _ in range(2):
        result = invoke("smoke", "--env-file", str(env), "--output", str(directory / "evidence"))
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["completed"] == 4
    for command in ("evaluate", "export", "export-evaluations", "report"):
        args = [command, "--env-file", str(env), "--run-id", "fabricated-smoke-v1"]
        if command != "evaluate":
            args += ["--output", str(directory / command)]
        result = invoke(*args)
        assert result.returncode == 0, result.stderr
    report = json.loads((directory / "report/report.json").read_text())
    assert report["run"]["provider_call_mode"] == "fake"
    assert report["run"]["execution_mode"] == "test"
    assert report["counts"]["expected"] == 4
    assert not any("100 fraud" in caveat for caveat in report["caveats"])
    with psycopg.connect(pg.owner_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM reckoner.attempts").fetchone() == (4,)
        for (document,) in connection.execute("SELECT document FROM reckoner.cohorts"):
            validate_cohort_manifest(document, SCHEMAS / "cohort-manifest-v1.schema.json")
    env.write_text(env.read_text() + "ANTHROPIC_API_KEY=fabricated-key-never-sent\n")
    result = invoke(
        "preflight",
        "--env-file",
        str(env),
        "--artifact-dir",
        "absent",
        "--config",
        "absent",
        "--purpose",
        "pilot",
        "--run-id",
        "must-not-dispatch",
    )
    assert result.returncode == 2
    assert "smoke database" in result.stderr


@pytest.mark.integration
def test_installed_smoke_refuses_non_smoke_database_before_mutation(installed_cli, pg):
    import psycopg

    invoke, directory = installed_cli
    env = directory / "normal-db.env"
    env.write_text(f"RECKONER_OWNER_DSN={pg.owner_dsn}\nRECKONER_RUNNER_DSN={pg.runner_dsn}\n")
    env.chmod(0o600)
    result = invoke("smoke", "--env-file", str(env), "--output", str(directory / "refused"))
    assert result.returncode == 2
    with psycopg.connect(pg.owner_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM reckoner.transactions").fetchone() == (0,)


@pytest.mark.integration
def test_installed_incomplete_report_returns_three(installed_cli, pg):
    from conftest import seed_run

    seed_run(pg, "unfinished", "tenant-a")
    invoke, directory = installed_cli
    env = directory / "incomplete.env"
    env.write_text(f"RECKONER_EVALUATOR_DSN={pg.evaluator_dsn}\n")
    result = invoke(
        "report",
        "--env-file",
        str(env),
        "--run-id",
        "unfinished",
        "--output",
        str(directory / "incomplete"),
    )
    assert result.returncode == 3, result.stderr


@pytest.mark.integration
def test_migrate_can_provision_dedicated_roles_from_protected_dsns(pg):
    from psycopg import connect, sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from psycopg.errors import InsufficientPrivilege
    from reckoner.storage.migrate import provision_roles

    values = conninfo_to_dict(pg.runner_dsn)
    role = values["user"] + "_new"
    dsn = make_conninfo(**{**values, "user": role})
    try:
        provision_roles(pg.owner_dsn, {"RECKONER_RUNNER_DSN": dsn})
        with connect(dsn) as connection:
            assert connection.execute("SELECT count(*) FROM reckoner.runs").fetchone() == (0,)
            with pytest.raises(InsufficientPrivilege):
                connection.execute("SELECT * FROM oracle.oracle_labels")
    finally:
        with connect(pg.owner_dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.mark.integration
def test_provision_roles_rejects_existing_oracle_membership(pg):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    from reckoner.storage.migrate import provision_roles

    role = conninfo_to_dict(pg.runner_dsn)["user"]
    with psycopg.connect(pg.owner_dsn) as connection:
        connection.execute(sql.SQL("GRANT reckoner_evaluator TO {}").format(sql.Identifier(role)))
    with pytest.raises(ValueError, match="privileges"):
        provision_roles(pg.owner_dsn, {"RECKONER_RUNNER_DSN": pg.runner_dsn})


@pytest.mark.integration
def test_cli_runner_contention_returns_safe_blocked_status(pg, monkeypatch, capsys):
    from reckoner import cli
    from reckoner.baseline.runner import preflight
    from reckoner.storage.postgres import PostgresRepository
    from test_runner import FakeProvider, _create_four_task_run

    _create_four_task_run(pg, "cli-busy", task_limit=1)
    provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        preflight(repo, provider, "cli-busy")
    monkeypatch.setattr(
        cli, "_prepare_run", lambda *_: (PostgresRepository(pg.runner_dsn), provider)
    )
    with PostgresRepository(pg.runner_dsn) as holder, holder.exclusive_runner():
        code = cli.main(
            [
                "run",
                "--env-file",
                "-",
                "--artifact-dir",
                "unused",
                "--config",
                "unused",
                "--purpose",
                "pilot",
                "--run-id",
                "cli-busy",
                "--allow-paid",
            ]
        )
    assert code == 3
    captured = capsys.readouterr()
    assert "blocked" in captured.err
    assert "Traceback" not in captured.err
    assert pg.runner_dsn not in captured.err
    assert provider.generation_calls == 0

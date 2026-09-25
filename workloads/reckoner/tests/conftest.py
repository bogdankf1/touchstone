from __future__ import annotations

import json
import os
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from reckoner.baseline.config import load_config
from reckoner.data.cohort import prepare
from test_cohort import write_source

ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = ROOT / "workloads/reckoner/config"


@dataclass(frozen=True)
class PostgresFixture:
    owner_dsn: str
    runner_dsn: str
    evaluator_dsn: str
    api_dsn: str
    bundle: Path


@pytest.fixture(scope="session")
def fabricated_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("postgres-bundle")
    source = root / "source"
    write_source(source)
    bundle = root / "bundle"
    prepare(source, bundle)
    return bundle


def _role_dsn(owner_dsn: str, database: str, role: str, password: str) -> str:
    values = conninfo_to_dict(owner_dsn)
    values.update(dbname=database, user=role, password=password)
    return make_conninfo(**values)


@pytest.fixture
def pg(fabricated_bundle):
    from reckoner.storage.migrate import migrate

    admin_dsn = os.environ.get("RECKONER_TEST_OWNER_DSN")
    if not admin_dsn:
        pytest.fail("integration test requires RECKONER_TEST_OWNER_DSN")

    suffix = uuid.uuid4().hex[:12]
    database = f"reckoner_test_{suffix}"
    login_roles = {
        kind: f"reckoner_test_{kind}_{suffix}" for kind in ("runner", "evaluator", "api")
    }
    passwords = {kind: secrets.token_urlsafe(24) for kind in login_roles}
    admin_values = conninfo_to_dict(admin_dsn)
    maintenance_dsn = make_conninfo(
        **{**admin_values, "dbname": admin_values.get("dbname", "postgres")}
    )
    with psycopg.connect(maintenance_dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    owner_dsn = make_conninfo(**{**admin_values, "dbname": database})

    try:
        migrate(owner_dsn)
        with psycopg.connect(maintenance_dsn, autocommit=True) as connection:
            for kind, role in login_roles.items():
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(passwords[kind])
                    )
                )
                connection.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(f"reckoner_{kind}"), sql.Identifier(role)
                    )
                )
        fixture = PostgresFixture(
            owner_dsn=owner_dsn,
            runner_dsn=_role_dsn(admin_dsn, database, login_roles["runner"], passwords["runner"]),
            evaluator_dsn=_role_dsn(
                admin_dsn, database, login_roles["evaluator"], passwords["evaluator"]
            ),
            api_dsn=_role_dsn(admin_dsn, database, login_roles["api"], passwords["api"]),
            bundle=fabricated_bundle,
        )
        yield fixture
    finally:
        with psycopg.connect(maintenance_dsn, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database))
            )
            for role in login_roles.values():
                connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def seed_run(pg: PostgresFixture, run_id: str, tenant_id: str, purpose: str = "baseline"):
    from reckoner.storage.postgres import PostgresRepository

    config = load_config(CONFIG_DIR / "baseline-v1.json", CONFIG_DIR / "anthropic-prices-v1.json")
    with PostgresRepository(pg.owner_dsn) as repo:
        repo.import_bundle(pg.bundle)
        for name in ("thresholds-tenant-a-v1.json", "thresholds-tenant-b-v1.json"):
            repo.register_threshold_config(json.loads((CONFIG_DIR / name).read_text()))
        repo.create_run(
            run_id,
            purpose,
            config,
            json.loads((pg.bundle / "bundle.json").read_text())["bundle_id"],
            price=json.loads((CONFIG_DIR / "anthropic-prices-v1.json").read_text()),
        )
    with PostgresRepository(pg.runner_dsn) as repo:
        return next(task for task in repo.pending_tasks(run_id) if task["tenant_id"] == tenant_id)

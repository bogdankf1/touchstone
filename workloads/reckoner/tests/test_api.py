from __future__ import annotations

import importlib

import psycopg
import pytest
from fastapi.testclient import TestClient
from reckoner.storage.postgres import PostgresRepository
from test_runner import FakeProvider, _create_four_task_run

pytestmark = pytest.mark.integration


def _evaluated_run(pg, run_id: str) -> None:
    runner = importlib.import_module("reckoner.baseline.runner")
    evaluate = importlib.import_module("reckoner.baseline.evaluate")
    _create_four_task_run(pg, run_id)
    with PostgresRepository(pg.runner_dsn) as repo:
        provider = FakeProvider()
        runner.preflight(repo, provider, run_id)
        runner.execute_run(repo, provider, run_id)
    with PostgresRepository(pg.evaluator_dsn) as repo:
        evaluate.evaluate_run(repo, run_id)


def test_readiness_uses_api_role_and_liveness_is_independent(pg):
    create_app = importlib.import_module("reckoner.app").create_app
    with TestClient(create_app(pg.api_dsn)) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "migration": "004_evaluation_reporting.sql"}

    with psycopg.connect(pg.owner_dsn) as owner:
        owner.execute(
            "DELETE FROM public.reckoner_schema_migrations WHERE version = %s",
            ("004_evaluation_reporting.sql",),
        )
    with TestClient(create_app(pg.api_dsn)) as client:
        incompatible = client.get("/health/ready")
    assert incompatible.status_code == 503
    assert incompatible.json() == {"detail": "database unavailable"}

    with TestClient(create_app("postgresql://nobody:dsn-secret@127.0.0.1:1/missing")) as client:
        assert client.get("/health/live").status_code == 200
        unavailable = client.get("/health/ready")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "database unavailable"}
    assert "dsn-secret" not in unavailable.text


def test_tenant_scoped_run_and_results_are_sanitized_and_paginated(pg):
    create_app = importlib.import_module("reckoner.app").create_app
    _evaluated_run(pg, "api-run")
    with psycopg.connect(pg.owner_dsn) as owner:
        tenant_id = owner.execute(
            "SELECT tenant_id FROM reckoner.tasks WHERE run_id = %s ORDER BY tenant_id LIMIT 1",
            ("api-run",),
        ).fetchone()[0]

    with TestClient(create_app(pg.api_dsn)) as client:
        run_response = client.get(f"/tenants/{tenant_id}/runs/api-run")
        results_response = client.get(f"/tenants/{tenant_id}/runs/api-run/results?limit=1&offset=0")
        missing = client.get("/tenants/not-the-tenant/runs/api-run")
        injected = client.get("/tenants/%27%20OR%20true--/runs/api-run")
        too_many = client.get(f"/tenants/{tenant_id}/runs/api-run/results?limit=101")
        negative = client.get(f"/tenants/{tenant_id}/runs/api-run/results?offset=-1")

    assert run_response.status_code == 200
    assert run_response.json()["metrics"]["correctness"]["denominator"] > 0
    assert results_response.status_code == 200
    assert len(results_response.json()["items"]) == 1
    rendered = (run_response.text + results_response.text).lower()
    for forbidden in (
        "oracle",
        "label",
        "password",
        "postgresql://",
        "prompt",
        "response_document",
    ):
        assert forbidden not in rendered
    assert missing.status_code == 404
    assert injected.status_code == 404
    assert too_many.status_code == 422
    assert negative.status_code == 422


def test_correctness_is_unavailable_until_all_fixed_denominator_cases_are_evaluated(pg):
    create_app = importlib.import_module("reckoner.app").create_app
    _create_four_task_run(pg, "api-unpublished")
    with psycopg.connect(pg.owner_dsn) as owner:
        tenant_id = owner.execute(
            "SELECT tenant_id FROM reckoner.tasks WHERE run_id = %s ORDER BY tenant_id LIMIT 1",
            ("api-unpublished",),
        ).fetchone()[0]

    with TestClient(create_app(pg.api_dsn)) as client:
        response = client.get(f"/tenants/{tenant_id}/runs/api-unpublished")

    assert response.status_code == 200
    assert response.json()["metrics"]["correctness"] is None


def test_api_role_cannot_read_operational_or_oracle_tables(pg):
    with psycopg.connect(pg.api_dsn) as connection:
        connection.execute("SELECT * FROM reckoner.api_run_summaries").fetchall()
        connection.execute("SELECT * FROM reckoner.api_results").fetchall()
        for table in (
            "reckoner.transactions",
            "reckoner.attempts",
            "reckoner.evaluations",
            "oracle.oracle_labels",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(f"SELECT * FROM {table}").fetchall()
            connection.rollback()

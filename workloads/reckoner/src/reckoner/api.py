"""Read-only, tenant-scoped operational API."""

from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import FastAPI, HTTPException, Query

from reckoner.storage.postgres import PostgresRepository


def create_app(api_dsn: str | None = None) -> FastAPI:
    application = FastAPI(title="Reckoner", docs_url=None, redoc_url=None, openapi_url=None)

    @application.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    def ready() -> dict[str, str]:
        if not api_dsn:
            raise HTTPException(status_code=503, detail="database unavailable")
        try:
            with PostgresRepository(api_dsn) as repository:
                migration = repository.readiness()
        except (psycopg.Error, ValueError):
            raise HTTPException(status_code=503, detail="database unavailable") from None
        return {"status": "ready", "migration": migration}

    @application.get("/tenants/{tenant_id}/runs/{run_id}")
    def run(tenant_id: str, run_id: str) -> dict:
        if not api_dsn:
            raise HTTPException(status_code=503, detail="database unavailable")
        try:
            with PostgresRepository(api_dsn) as repository:
                result = repository.api_run_summary(tenant_id, run_id)
        except psycopg.Error:
            raise HTTPException(status_code=503, detail="database unavailable") from None
        if result is None:
            raise HTTPException(status_code=404, detail="run not found")
        return result

    @application.get("/tenants/{tenant_id}/runs/{run_id}/results")
    def results(
        tenant_id: str,
        run_id: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict:
        if not api_dsn:
            raise HTTPException(status_code=503, detail="database unavailable")
        try:
            with PostgresRepository(api_dsn) as repository:
                items = repository.api_results(tenant_id, run_id, limit=limit, offset=offset)
        except psycopg.Error:
            raise HTTPException(status_code=503, detail="database unavailable") from None
        if items is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {"items": items, "limit": limit, "offset": offset}

    return application

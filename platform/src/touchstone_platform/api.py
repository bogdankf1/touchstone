"""Read-only HTTP access to published governed Touchstone results."""

from __future__ import annotations

from contextlib import contextmanager

import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from touchstone_platform.query import open_snapshot
from touchstone_platform.settings import Settings


@contextmanager
def _snapshot(settings):
    try:
        with open_snapshot(settings) as snapshot:
            yield snapshot
    except (FileNotFoundError, ValueError, duckdb.Error) as error:
        raise HTTPException(status_code=503, detail="published snapshot unavailable") from error


def _tenant_selection(tenant_id: str | None, aggregate: bool):
    if aggregate and tenant_id is not None:
        raise HTTPException(status_code=422, detail="aggregate mode requires no tenant_id")
    if not aggregate and tenant_id is None:
        raise HTTPException(status_code=422, detail="tenant_id is required")


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Touchstone read API", version="1")

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            with open_snapshot(settings) as snapshot:
                snapshot.connection.execute("select 1").fetchone()
                return {"status": "ready", "metadata": snapshot.metadata()}
        except (FileNotFoundError, ValueError, duckdb.Error):
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "reason": "no published snapshot"},
            )

    @app.get("/v1/workflows")
    def workflows():
        with _snapshot(settings) as snapshot:
            return {"metadata": snapshot.metadata(), "data": snapshot.workflows()}

    @app.get("/v1/runs")
    def runs(workflow_id: str = Query(min_length=1), tenant_id: str = Query(min_length=1)):
        with _snapshot(settings) as snapshot:
            return {"metadata": snapshot.metadata(), "data": snapshot.runs(workflow_id, tenant_id)}

    @app.get("/v1/runs/{run_id:path}/summary")
    def summary(
        run_id: str,
        workflow_id: str = Query(min_length=1),
        tenant_id: str | None = Query(default=None, min_length=1),
        aggregate: bool = False,
    ):
        _tenant_selection(tenant_id, aggregate)
        with _snapshot(settings) as snapshot:
            result = snapshot.summary(workflow_id, run_id, tenant_id=tenant_id, aggregate=aggregate)
            if result is None:
                raise HTTPException(status_code=404, detail="run not found")
            return {"metadata": snapshot.metadata(), "data": result}

    @app.get("/v1/runs/{run_id:path}/tasks")
    def tasks(
        run_id: str,
        workflow_id: str = Query(min_length=1),
        tenant_id: str | None = Query(default=None, min_length=1),
        aggregate: bool = False,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=100),
    ):
        _tenant_selection(tenant_id, aggregate)
        with _snapshot(settings) as snapshot:
            selected = snapshot.summary(
                workflow_id, run_id, tenant_id=tenant_id, aggregate=aggregate
            )
            if selected is None:
                raise HTTPException(status_code=404, detail="run not found")
            return {
                "metadata": snapshot.metadata(),
                "data": snapshot.tasks(
                    workflow_id, run_id, selected["tenant_ids"], page, page_size
                ),
            }

    @app.get("/v1/traces/{trace_id}")
    def trace(
        trace_id: str,
        workflow_id: str = Query(min_length=1),
        run_id: str = Query(min_length=1),
        tenant_id: str = Query(min_length=1),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=100),
    ):
        with _snapshot(settings) as snapshot:
            result = snapshot.trace(trace_id, workflow_id, run_id, tenant_id, page, page_size)
            if result is None:
                raise HTTPException(status_code=404, detail="trace not found")
            return {"metadata": snapshot.metadata(), "data": result}

    return app

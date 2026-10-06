"""Public read API contracts over a pinned published warehouse."""

import json
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import validate
from touchstone_platform.api import create_app
from touchstone_platform.settings import Settings

pytest_plugins = ("tests.test_query",)


SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2] / "contracts/schemas/dashboard-response-v1.schema.json"
    ).read_text()
)


def assert_schema(response, kind):
    assert response.status_code == 200, response.text
    validate(response.json(), {**SCHEMA, "$ref": f"#/$defs/{kind}"})


def test_missing_snapshot_is_not_ready_and_has_safe_reason(tmp_path):
    client = TestClient(create_app(Settings(warehouse_dir=tmp_path)))
    assert client.get("/healthz").status_code == 200
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "reason": "no published snapshot"}
    assert client.get("/v1/workflows").status_code == 503


def test_read_endpoints_validate_schema_and_preserve_unknowns(published):
    client = TestClient(create_app(published))
    (published.warehouse_dir / "refresh-status.json").write_text(
        json.dumps(
            {
                "state": "failed",
                "attempted_at": "2026-09-26T10:04:00Z",
                "error": "credentials=secret token=hidden internal path /private/tmp/source",
            }
        )
    )
    ready = client.get("/readyz")
    assert_schema(ready, "ready")
    assert ready.json()["metadata"]["latest_refresh"]["state"] == "failed"
    assert "secret" not in ready.text
    assert_schema(client.get("/v1/workflows"), "workflows")
    assert_schema(
        client.get("/v1/runs", params={"workflow_id": "workflow", "tenant_id": "tenant-a"}), "runs"
    )
    summary = client.get(
        "/v1/runs/run/summary", params={"workflow_id": "workflow", "tenant_id": "tenant-a"}
    )
    assert_schema(summary, "summary")
    assert summary.json()["data"]["model_cost"] == "0.100000000001"
    assert summary.json()["data"]["cpst"] is None
    assert summary.json()["data"]["contribution_rates"][0]["rate"] is None
    assert_schema(
        client.get(
            "/v1/runs/run/tasks", params={"workflow_id": "workflow", "tenant_id": "tenant-a"}
        ),
        "tasks",
    )
    trace = client.get(
        "/v1/traces/shared-trace",
        params={"workflow_id": "workflow", "run_id": "run", "tenant_id": "tenant-a"},
    )
    assert_schema(trace, "trace")
    assert "private" not in trace.text


def test_filters_404_422_and_tenant_scoped_trace(published):
    client = TestClient(create_app(published))
    assert (
        client.get(
            "/v1/runs/missing/summary", params={"workflow_id": "workflow", "tenant_id": "tenant-a"}
        ).status_code
        == 404
    )
    assert client.get("/v1/runs/run/summary").status_code == 422
    assert (
        client.get(
            "/v1/runs/run/summary",
            params={"workflow_id": "workflow", "aggregate": "true", "tenant_id": "tenant-a"},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/v1/runs/run/tasks",
            params={"workflow_id": "workflow", "tenant_id": "tenant-a", "page_size": 101},
        ).status_code
        == 422
    )
    trace = client.get(
        "/v1/traces/shared-trace",
        params={"workflow_id": "workflow", "run_id": "run", "tenant_id": "tenant-a"},
    )
    assert [e["tenant_id"] for e in trace.json()["data"]["events"]] == ["tenant-a"]
    assert (
        client.get(
            "/v1/traces/shared-trace",
            params={"workflow_id": "workflow", "run_id": "run", "tenant_id": "tenant-x"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/v1/runs/run/summary",
            params={"workflow_id": "workflow", "tenant_id": "tenant-a' OR 1=1 --"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/v1/runs/run/summary", params={"workflow_id": "workflow", "tenant_id": "../tenant-b"}
        ).status_code
        == 404
    )


def test_explicit_aggregate_excludes_incompatible_run_and_retains_tenants(published):
    client = TestClient(create_app(published))
    response = client.get(
        "/v1/runs/run/summary", params={"workflow_id": "workflow", "aggregate": "true"}
    )
    assert_schema(response, "summary")
    data = response.json()["data"]
    assert data["tenant_ids"] == ["tenant-a", "tenant-b"]
    assert data["excluded_tenants"] == ["tenant-c"]
    assert data["latency_p99_ms"] == 1000.0
    assert data["model_cost"] == "0.300000000003"
    assert len(data["tenants"]) == 2


def test_workflow_discovery_exposes_tenant_ids_for_run_selection(published):
    response = TestClient(create_app(published)).get("/v1/workflows")
    assert_schema(response, "workflows")
    assert response.json()["data"] == [
        {
            "workflow_id": "workflow",
            "tenant_count": 3,
            "run_count": 4,
            "tenant_ids": ["tenant-a", "tenant-b", "tenant-c"],
        }
    ]


def test_invalid_manifest_is_safe_503_for_readiness_and_data(published):
    client = TestClient(create_app(published))
    manifest = published.warehouse_dir / "current.json"
    for contents in ("{broken", "{}", "[]", '{"generation": 17}'):
        manifest.write_text(contents)
        ready = client.get("/readyz")
        assert ready.status_code == 503
        assert ready.json() == {"status": "unavailable", "reason": "no published snapshot"}
        data = client.get("/v1/workflows")
        assert data.status_code == 503
        assert data.json() == {"detail": "published snapshot unavailable"}


def test_comparison_requires_declared_provenance_and_uses_one_generation(published):
    client = TestClient(create_app(published))
    response = client.get(
        "/v1/comparisons",
        params={
            "workflow_id": "workflow",
            "baseline": "run",
            "current": "run",
            "tenant_id": "tenant-a",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["eligible"] is False
    assert data["delta_cpst"] is None
    assert data["baseline"]["generation"] == data["current"]["generation"]
    assert "missing arm provenance" in data["reasons"]


def test_declared_compatible_comparison_is_served_from_real_snapshot(published):
    import duckdb

    from tests.test_comparison import result

    path = published.warehouse_dir / "generation-one.duckdb"
    with duckdb.connect(str(path)) as db:
        db.execute(
            "update mart_runs set metrics_complete=true where tenant_id='tenant-a' and run_id='run'"
        )
        document = {
            "code_revision": "rev1",
            "dataset_version": "data1",
            "expected_task_ids": ["a1", "a2"],
            "config_version": "cfg",
            "comparison": {
                "reference_version": "r",
                "business_config_id": "b",
                "arm": result()["arm_provenance"][0],
            },
        }
        db.execute(
            "update raw_declarations set document_json=? where tenant_id='tenant-a' and "
            "run_id='run'",
            [json.dumps(document)],
        )
        db.execute(
            "insert into mart_runs select * replace ('current' as run_id,0.200000000002 as "
            "model_cost) from mart_runs where tenant_id='tenant-a' and run_id='run'"
        )
        db.execute(
            "insert into raw_declarations select * replace ('current' as run_id,'newhash' as "
            "content_sha256) from raw_declarations where tenant_id='tenant-a' and "
            "run_id='run'"
        )
    (published.warehouse_dir / "refresh-status.json").write_text(json.dumps({"state": "succeeded"}))
    client = TestClient(create_app(published))
    response = client.get(
        "/v1/comparisons",
        params={
            "workflow_id": "workflow",
            "baseline": "run",
            "current": "current",
            "tenant_id": "tenant-a",
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert_schema(response, "comparison")
    assert data["eligible"] is True, data["reasons"]
    assert data["delta_cpst"] == "0.0500000000005"
    assert data["baseline"]["case_membership"] == [["tenant-a", "a1"], ["tenant-a", "a2"]]
    assert data["baseline"]["arm_provenance"][0]["call_ids"] == ["actual-source-call"]
    assert data["current"]["arm_provenance"][0]["call_ids"] == []

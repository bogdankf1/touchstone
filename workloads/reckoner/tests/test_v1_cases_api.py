"""Operational projections on simulated fixtures, with opaque query identities."""

import json

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from reckoner.api import create_app
from reckoner.v1.storage.repository import V1Repository
from test_v1_storage import setup_run
from v1_fixtures import decision_fixture, evidence_fixture

pytestmark = pytest.mark.integration


def case_setup(pg):
    config, manifest, task = setup_run(pg)
    evidence = evidence_fixture(transaction_id=task["transaction"]["transaction_id"])
    decision = decision_fixture(config, evidence, degraded=True)
    with V1Repository(pg.runner_dsn) as repo:
        repo.persist_evidence(evidence)
        repo.persist_decision(decision)
    return decision


def test_case_projection_filters_and_missing_note(pg):
    decision = case_setup(pg)
    with TestClient(create_app(pg.api_dsn)) as client:
        queue = client.get(
            "/v1/cases", params={"tenant_id": "tenant-a", "status": "open", "limit": 1}
        )
        assert queue.status_code == 200
        item = queue.json()["items"][0]
        assert item["case_id"] == decision["decision_id"]
        assert item["status"] == "open"
        assert item["note_status"] == "pending"
        assert item["degraded"] is True
        assert item["version"] == 1
        detail = client.get(
            "/v1/case", params={"tenant_id": "tenant-a", "case_id": item["case_id"]}
        )
        assert detail.status_code == 200
        assert detail.json()["note"] is None
        assert detail.json()["graph"]["status"] == "unavailable"
        for status in ("resolved", "note_failed"):
            assert (
                client.get("/v1/cases", params={"tenant_id": "tenant-a", "status": status}).json()[
                    "items"
                ]
                == []
            )
        for status in ("note_pending", "degraded", "all"):
            assert (
                len(
                    client.get(
                        "/v1/cases", params={"tenant_id": "tenant-a", "status": status}
                    ).json()["items"]
                )
                == 1
            )
        assert (
            client.get(
                "/v1/case", params={"tenant_id": "tenant/b", "case_id": item["case_id"]}
            ).status_code
            == 404
        )
        for params in (
            {"limit": 0},
            {"limit": 101},
            {"cursor": "not-valid"},
            {"status": "bogus"},
            {"tenant_id": ""},
        ):
            assert (
                client.get("/v1/cases", params={"tenant_id": "tenant-a", **params}).status_code
                == 422
            )
        assert (
            client.get(
                "/v1/cases", params={"tenant_id": "tenant-a", "run_id": "missing/run"}
            ).json()["items"]
            == []
        )
    for forbidden in ("oracle", "password", "provider_request", "provider_response"):
        assert forbidden not in detail.text.lower()


def test_opaque_ids_and_safe_actual_late_note(pg):
    config, _, task = setup_run(pg)
    evidence = evidence_fixture(transaction_id=task["transaction"]["transaction_id"])
    decision = decision_fixture(config, evidence, degraded=True)
    with V1Repository(pg.owner_dsn) as owner:
        owner.persist_evidence(evidence)
        keys = (
            "tenant_id",
            "run_id",
            "task_id",
            "transaction_id",
            "decision_id",
            "config_id",
            "evidence_id",
            "outcome",
            "scorer_status",
            "call_id",
            "completed_at",
        )
        owner._insert(
            "v1_decisions",
            {**{k: decision[k] for k in keys}, "document": decision},
            {"tenant_id": "tenant-a", "decision_id": decision["decision_id"]},
        )
        case = {
            "tenant_id": "tenant-a",
            "case_id": "case/with/slashes",
            "decision_id": decision["decision_id"],
        }
        owner._insert(
            "v1_cases",
            {**case, "document": case},
            {"tenant_id": "tenant-a", "case_id": case["case_id"]},
        )
    with psycopg.connect(pg.owner_dsn) as owner:
        doc = {
            "tenant_id": "tenant-a",
            "case_id": "case/with/slashes",
            "run_id": "run-a",
            "task_id": "task-a",
            "status": "pending",
        }
        owner.execute(
            "INSERT INTO reckoner.v1_note_work VALUES (%s,%s,%s,%s,%s)",
            ("tenant-a", doc["case_id"], "run-a", "task-a", Jsonb(doc)),
        )
        result = {
            **doc,
            "status": "succeeded",
            "available_before_review": False,
            "note": {"verdict_recommendation": "approve", "provider_response": "secret"},
            "provider_request": "secret",
        }
        owner.execute(
            "INSERT INTO reckoner.v1_note_results VALUES (%s,%s,%s)",
            ("tenant-a", doc["case_id"], Jsonb(result)),
        )
    with TestClient(create_app(pg.api_dsn)) as client:
        response = client.get(
            "/v1/case", params={"tenant_id": "tenant-a", "case_id": "case/with/slashes"}
        )
        assert response.status_code == 200
        assert response.json()["note"]["verdict_recommendation"] == "approve"
        assert response.json()["available_before_review"] is False
        assert "secret" not in response.text
        assert "provider_request" not in json.dumps(create_app(pg.api_dsn).openapi())


def test_query_ids_pagination_scope_and_bounded_graph(pg):
    from conftest import CONFIG_DIR
    from v1_fixtures import config_fixture, experiment_fixture, identified

    config, _, task = setup_run(pg)
    tenant = "tenant/opaque"
    threshold = json.loads((CONFIG_DIR / "thresholds-tenant-a-v1.json").read_text())
    threshold["tenant_id"] = tenant
    identified(threshold, "config_id")
    config = config_fixture(tenant, threshold["config_id"])
    transaction = {**task["transaction"], "tenant_id": tenant}
    with psycopg.connect(pg.owner_dsn) as owner:
        owner.execute(
            "INSERT INTO reckoner.transactions VALUES(%s,%s,%s)",
            (tenant, transaction["transaction_id"], Jsonb(transaction)),
        )
        owner.execute(
            "INSERT INTO reckoner.threshold_configs VALUES(%s,%s,%s)",
            (tenant, threshold["config_id"], Jsonb(threshold)),
        )
    with V1Repository(pg.owner_dsn) as owner:
        owner.register_config(config)
        for index in range(3):
            manifest = experiment_fixture(config, transaction["transaction_id"])
            manifest["run_id"] = f"run/opaque/{index}"
            identified(manifest, "experiment_id")
            owner.create_run(manifest, config["config_id"])
            evidence = evidence_fixture(tenant, transaction["transaction_id"])
            if index == 0:
                evidence["neighbourhood"] = {
                    "nodes": [
                        {
                            "id": str(i),
                            "tenant_id": tenant,
                            "kind": "account",
                            "identity": str(i),
                            "provider_response": "hidden",
                        }
                        for i in range(105)
                    ],
                    "edges": [],
                    "total_nodes": 105,
                    "total_edges": 0,
                    "truncated": False,
                    "cross_tenant": False,
                    "transaction_refs": ["hidden"] * 10000,
                }
                identified(evidence, "evidence_id")
                owner._insert(
                    "v1_evidence",
                    {
                        "tenant_id": tenant,
                        "evidence_id": evidence["evidence_id"],
                        "transaction_id": transaction["transaction_id"],
                        "query_time": evidence["query_time"],
                        "coverage_status": "available",
                        "document": evidence,
                    },
                    {"tenant_id": tenant, "evidence_id": evidence["evidence_id"]},
                )
            else:
                owner.persist_evidence(evidence)
            decision = decision_fixture(config, evidence, degraded=True)
            decision["run_id"] = manifest["run_id"]
            identified(decision, "decision_id")
            owner.persist_decision(decision)
    with TestClient(create_app(pg.api_dsn)) as client:
        params = {"tenant_id": tenant, "status": "all", "limit": 1}
        items = []
        for _ in range(3):
            page = client.get("/v1/cases", params=params).json()
            items.extend(page["items"])
            if page["next_cursor"]:
                params["cursor"] = page["next_cursor"]
        assert len({row["case_id"] for row in items}) == 3
        assert page["next_cursor"] is None
        assert (
            client.get("/v1/cases", params={**params, "tenant_id": "tenant-a"}).status_code == 422
        )
        page = client.get(
            "/v1/cases", params={"tenant_id": tenant, "run_id": "run/opaque/0"}
        ).json()
        assert len(page["items"]) == 1
        detail = client.get(
            "/v1/case", params={"tenant_id": tenant, "case_id": page["items"][0]["case_id"]}
        ).json()
        assert len(detail["graph"]["nodes"]) == 100
        assert detail["graph"]["truncated"] is True
        assert "hidden" not in json.dumps(detail)


def test_failed_note_is_visible_and_does_not_block_review(pg):
    decision = case_setup(pg)
    result = {
        "tenant_id": "tenant-a",
        "case_id": decision["decision_id"],
        "status": "uncertain",
        "note": None,
        "available_before_review": True,
        "attempts": [{"provider_response": "secret"}],
    }
    with psycopg.connect(pg.owner_dsn) as owner:
        owner.execute(
            "INSERT INTO reckoner.v1_note_results VALUES(%s,%s,%s)",
            ("tenant-a", decision["decision_id"], Jsonb(result)),
        )
    with TestClient(create_app(pg.api_dsn)) as client:
        page = client.get(
            "/v1/cases", params={"tenant_id": "tenant-a", "status": "note_failed"}
        ).json()
        assert len(page["items"]) == 1
        assert page["items"][0]["note_status"] == "uncertain"
        from test_v1_reviews_api import action

        assert client.post("/v1/reviews", json=action(decision["decision_id"])).status_code == 200


def test_openapi_declares_operational_response_fields():
    schema = create_app().openapi()
    response = schema["paths"]["/v1/case"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    assert "$ref" in response
    detail = schema["components"]["schemas"][response["$ref"].rsplit("/", 1)[-1]]
    assert {"case_id", "version", "note_status", "note", "review", "graph"} <= set(
        detail["properties"]
    )
    assert "provider_response" not in json.dumps(schema)

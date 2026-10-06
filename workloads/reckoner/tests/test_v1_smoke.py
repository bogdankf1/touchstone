"""Fabricated v1 deployment smoke: deterministic source, smoke-only targets, no provider."""

import importlib
from datetime import datetime

import psycopg
import pytest

TENANTS = ("tenant-a", "tenant-b")


def smoke():
    try:
        return importlib.import_module("reckoner.v1.smoke")
    except ModuleNotFoundError:
        pytest.fail("v1 deployment smoke is not implemented")


def test_fabricated_source_is_deterministic_simulated_and_time_valid():
    module = smoke()
    first, second = module.fabricated_source(), module.fabricated_source()
    assert first == second
    assert {tx["tenant_id"] for tx in first["queries"]} == set(TENANTS)
    assert len(first["queries"]) == 4
    earliest = min(datetime.fromisoformat(tx["occurred_at"]) for tx in first["queries"])
    for record in first["records"]:
        assert datetime.fromisoformat(record["resolution"]["resolved_at"]) < earliest
        assert record["resolution"]["provenance"]["dataset_simulated"] is True
        assert all(
            prior["occurred_at"] < record["transaction"]["occurred_at"]
            for prior in record["history"]["transactions"]
        )
    shared = {}
    for item in first["identities"]:
        shared.setdefault(item["identity"], set()).add(item["tenant_id"])
    assert any(tenants == set(TENANTS) for tenants in shared.values())
    assert {c["source_snapshot_id"] for c in first["coverage"]} == {first["source_id"]}
    for tenant in TENANTS:
        manifest = module.smoke_manifest(tenant, module.smoke_config(tenant, "s" * 64), first)
        assert manifest["purpose"] == "fabricated" and manifest["dataset_simulated"] is True
        assert manifest["dataset_version"] == first["source_id"]


@pytest.mark.parametrize("step", ["seed", "evidence", "run"])
def test_smoke_cli_refuses_a_non_smoke_database_before_connecting(step, monkeypatch, capsys):
    from reckoner.cli import main

    smoke()

    def refuse(*args, **kwargs):
        raise AssertionError("store contacted before the smoke-target check")

    monkeypatch.setattr(psycopg, "connect", refuse)
    import neo4j

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", refuse)
    dsn = "postgresql://user:secret@127.0.0.1:1/reckoner_measured"
    for name in ("RECKONER_OWNER_DSN", "RECKONER_RUNNER_DSN"):
        monkeypatch.setenv(name, dsn)
    monkeypatch.setenv("RECKONER_NEO4J_URI", "bolt://127.0.0.1:1")
    monkeypatch.setenv("RECKONER_NEO4J_USER", "neo4j")
    monkeypatch.setenv("RECKONER_NEO4J_PASSWORD", "fabricated")
    assert main(["v1", "smoke", step, "--env-file", "-"]) == 2
    error = capsys.readouterr().err
    assert "reckoner_smoke_" in error and "secret" not in error


def test_smoke_env_file_accepts_only_named_smoke_keys(tmp_path, capsys):
    from reckoner.cli import main

    smoke()
    path = tmp_path / "smoke.env"
    path.write_text("RECKONER_OWNER_DSN=postgresql://x/reckoner_smoke_a\nJEV_API_KEY=nope\n")
    assert main(["v1", "smoke", "seed", "--env-file", str(path)]) == 2
    assert "nope" not in capsys.readouterr().err


def _clear(driver):
    smoke().clear_smoke_graph(driver)  # refuses any store that is not marked as owned


def _source_graph(driver):
    with driver.session() as session:
        return sorted(
            (r["labels"], r["document"], r["resolution"])
            for r in session.run(
                "MATCH (n) WHERE n.tenant_id IN $tenants AND NOT n:ProjectionReceipt "
                "AND NOT n:GDSMetric RETURN labels(n) AS labels, n.document AS document, "
                "n.resolution AS resolution",
                tenants=list(TENANTS),
            )
        )


DETERMINISTIC = (
    "node_count",
    "edge_count",
    "covered_accounts",
    "covered_cards",
    "covered_merchants",
    "community_count",
    "page_rank_converged",
    "page_rank_iterations",
    "cutoff",
)


@pytest.mark.integration
@pytest.mark.neo4j_integration
@pytest.mark.parametrize("pg", ["smoke"], indirect=True)
def test_smoke_graph_reimport_and_degraded_run_without_graph(pg, graph_driver):
    from fastapi.testclient import TestClient
    from reckoner.app import create_app

    module = smoke()
    _clear(graph_driver)
    seeded = module.seed_postgres(pg.owner_dsn)
    assert seeded["tasks"] == 4 and seeded["provider_calls"] == 0
    assert module.seed_postgres(pg.owner_dsn) == seeded  # identical retry is harmless
    graph = module.seed_graph(graph_driver)
    assert graph["projection"]["node_count"] > 0
    before = _source_graph(graph_driver)

    available = module.graph_evidence(pg.runner_dsn, graph_driver)
    assert available["workflow_executed"] is False
    assert {e["coverage"]["status"] for e in available["evidence"]} == {"available"}
    assert all(
        e["graph_snapshot"] == graph["projection"]["projection_id"] for e in available["evidence"]
    )

    # Graph reimport from the immutable fabricated source into an emptied graph.
    _clear(graph_driver)
    assert _source_graph(graph_driver) == []
    again = module.seed_graph(graph_driver)
    assert _source_graph(graph_driver) == before
    assert {k: again["projection"][k] for k in DETERMINISTIC} == {
        k: graph["projection"][k] for k in DETERMINISTIC
    }

    result = module.run_without_graph(pg.runner_dsn)
    assert len(result["decisions"]) == 4
    for decision in result["decisions"]:
        assert decision["outcome"] == "escalate"
        assert decision["scorer_status"] == "unavailable"
        assert decision["degraded_reason"] == "evidence_unavailable"
        assert "graph unavailable" in decision["missing"]
    assert module.run_without_graph(pg.runner_dsn) == result  # restart creates no second decision
    with psycopg.connect(pg.owner_dsn) as owner:
        assert owner.execute("SELECT count(*) FROM reckoner.v1_decisions").fetchone()[0] == 4
        assert owner.execute("SELECT count(*) FROM reckoner.v1_provider_calls").fetchone()[0] == 0
    with TestClient(create_app(pg.api_dsn)) as client:
        assert client.get("/health/ready").status_code == 200
        queue = client.get("/v1/cases", params={"tenant_id": "tenant-a", "status": "all"})
        assert queue.status_code == 200 and len(queue.json()["items"]) == 2
        case = queue.json()["items"][0]["case_id"]
        detail = client.get("/v1/case", params={"tenant_id": "tenant-a", "case_id": case})
        # The display neighbourhood is the relational baseline; the stopped graph service
        # shows as missing coverage and a null graph snapshot, which the console renders.
        assert "graph unavailable" in detail.json()["coverage"]["missing"]
        assert detail.json()["source_snapshot_ids"]["graph"] is None
        assert detail.json()["degraded_reason"] == "evidence_unavailable"
        foreign = client.get("/v1/case", params={"tenant_id": "tenant-b", "case_id": case})
        assert foreign.status_code == 404
    _clear(graph_driver)


@pytest.mark.integration
@pytest.mark.neo4j_integration
@pytest.mark.parametrize("pg", ["smoke"], indirect=True)
def test_smoke_refuses_foreign_experiments_and_foreign_graph_tenants(pg, graph_driver):
    module = smoke()
    with graph_driver.session() as session:
        session.run(
            "CREATE (:Transaction {tenant_id:$tenant})", tenant=graph_driver.test_tenants[0]
        ).consume()
    with pytest.raises(ValueError, match="non-smoke"):
        module.seed_graph(graph_driver)
    module.seed_postgres(pg.owner_dsn)
    from reckoner.v1.storage.repository import V1Repository

    source = module.fabricated_source()
    config = module.smoke_config("tenant-a", module.smoke_scaler()["scaler_id"])
    other = {**module.smoke_manifest("tenant-a", config, source), "run_id": "measured-run"}
    other.pop("experiment_id")
    from reckoner.contracts import content_id

    other["experiment_id"] = content_id(other)
    with V1Repository(pg.owner_dsn) as repo:
        repo.create_run(other, config["config_id"])
    with pytest.raises(ValueError, match="another experiment"):
        module.seed_postgres(pg.owner_dsn)


class FakeGraph:
    """Records every Cypher statement; answers the ownership and size probes."""

    def __init__(self, owned=0, total=0):
        self.owned, self.total, self.statements = owned, total, []

    def session(self, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def run(self, statement, **params):
        self.statements.append(statement)
        graph = self

        class Result:
            def single(self):
                if "SmokeStore OR" in statement:
                    return {"n": graph.owned}
                if statement.startswith("MATCH (n) RETURN count(n)"):
                    return {"n": graph.total}
                return {"n": 0}

            def consume(self):
                pass

        return Result()


WRITES = ("CREATE", "MERGE", "DELETE", "SET", "CALL gds")


def test_seed_graph_refuses_an_unmarked_graph_holding_real_tenant_names_without_writing():
    """tenant-a/tenant-b are also real simulated-archive tenants: a name is not ownership."""
    module = smoke()
    graph = FakeGraph(owned=0, total=124_000)
    with pytest.raises(ValueError, match="not an owned"):
        module.seed_graph(graph)
    with pytest.raises(ValueError, match="not an owned"):
        module.clear_smoke_graph(graph)
    assert not [s for s in graph.statements if any(w in s for w in WRITES)]


def test_seed_graph_claims_only_an_empty_graph():
    module = smoke()
    graph = FakeGraph(owned=0, total=0)
    module.require_owned_graph(graph, claim=True)
    assert any(s.startswith("CREATE (:SmokeStore") for s in graph.statements)
    marked = FakeGraph(owned=1, total=500)
    module.require_owned_graph(marked)
    assert not [s for s in marked.statements if any(w in s for w in WRITES)]

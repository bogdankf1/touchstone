import pytest
from evidence_fixtures import coverage, records
from reckoner.v1.evidence.neo4j import Neo4jEvidence, import_graph
from reckoner.v1.evidence.postgres import PostgresEvidence, import_evidence
from test_v1_retrieval import config


@pytest.mark.neo4j_integration
@pytest.mark.integration
def test_sql_cypher_equal_sets_daily_gds_and_temporal_invariance(pg, graph_driver):
    query, rows, identities = records(graph_driver.test_tenants)
    cfg = config()
    task = {"transaction": query, "query_time": query["occurred_at"]}
    import_evidence(
        pg.owner_dsn, [query], rows, identities, coverage(graph_driver.test_tenants), cfg["scaler"]
    )
    import_graph(graph_driver, rows, identities, coverage(graph_driver.test_tenants))
    graph = Neo4jEvidence(graph_driver)
    sql = PostgresEvidence(pg.runner_dsn).for_task(task, cfg)
    cypher = graph.for_task(task, cfg)
    assert sql["neighbourhood"] == cypher["neighbourhood"]
    receipt = graph.project("2018-06-01T00:00:00Z", list(graph_driver.test_tenants))
    assert receipt["parameters"]["concurrency"] == 1
    assert receipt["parameters"]["page_rank"]["dampingFactor"] == 0.85
    assert receipt["gds_version"] == "2026.09.0"
    assert receipt["node_count"] > 0 and receipt["edge_count"] > 0
    earlier = graph.for_task(task, cfg)
    assert earlier["graph_projection"]["cutoff"] == "2018-06-01T00:00:00Z"
    assert earlier["graph_projection"]["snapshot_age_seconds"] == "43200.0"
    from reckoner.v1.data.history import historical_resolution

    future = {**query, "transaction_id": "future-extra", "occurred_at": "2018-06-03T00:00:00Z"}
    import_graph(
        graph_driver,
        [
            {
                "transaction": future,
                "resolution": historical_resolution(future, "fraud", "simulated-seven-days-v1"),
            }
        ],
        identities,
        [],
    )
    assert earlier == graph.for_task(task, cfg)
    graph.project("2018-06-02T00:00:00Z", list(graph_driver.test_tenants))
    assert earlier == graph.for_task(task, cfg)
    with graph_driver.session() as session:
        assert (
            session.run("CALL gds.graph.list() YIELD graphName RETURN count(*) AS n").single()["n"]
            == 0
        )


@pytest.mark.neo4j_integration
def test_verified_empty_and_unavailable_graph_are_distinct(graph_driver):
    query, _, _ = records(graph_driver.test_tenants)
    graph = Neo4jEvidence(graph_driver)
    task = {"transaction": query, "query_time": query["occurred_at"]}
    assert graph.for_task(task, config())["coverage"]["status"] == "unavailable"
    import_graph(
        graph_driver,
        [],
        [
            {
                "tenant_id": query["tenant_id"],
                "merchant_id": query["merchant_id"],
                "identity": "empty-only",
            }
        ],
        coverage(graph_driver.test_tenants),
    )
    result = graph.for_task(task, config())
    assert result["neighbourhood"]["total_transactions"] == 0
    assert result["coverage"]["status"] == "partial"  # no GDS snapshot yet
    assert result["neighbourhood"]["truncated"] is False


@pytest.mark.integration
@pytest.mark.neo4j_integration
def test_paired_resolution_sets_use_exact_seven_day_availability(pg, graph_driver):
    query, rows, identities = records(graph_driver.test_tenants)
    cfg = config()
    import_evidence(
        pg.owner_dsn, [query], rows, identities, coverage(graph_driver.test_tenants), cfg["scaler"]
    )
    import_graph(graph_driver, rows, identities, coverage(graph_driver.test_tenants))
    task = {"transaction": query}
    sql = PostgresEvidence(pg.runner_dsn).resolved_cases(task)
    cypher = Neo4jEvidence(graph_driver).resolved_cases(task)
    assert sql == cypher
    assert {r["transaction_id"] for r in sql} == {"a", "b", "c", "d", "e", "z"}


@pytest.mark.neo4j_integration
def test_import_only_maintains_incoming_shared_merchant_identities(graph_driver):
    from reckoner.v1.data.history import historical_resolution

    query, rows, identities = records(graph_driver.test_tenants)
    import_graph(graph_driver, rows, identities, coverage(graph_driver.test_tenants))
    with graph_driver.session() as session:
        session.run(
            "MATCH(a:Merchant {tenant_id:$tenant})-[r:SHARED_IDENTITY]->(b) "
            "SET r.occurred_at=datetime('2018-05-22T00:00:00Z')",
            tenant=query["tenant_id"],
        ).consume()
    unrelated = {**query, "transaction_id": "unrelated", "merchant_id": "unrelated-merchant"}
    item = {
        "transaction": unrelated,
        "resolution": historical_resolution(unrelated, "legitimate", "simulated-seven-days-v1"),
    }
    import_graph(
        graph_driver,
        [item],
        [
            {
                "tenant_id": query["tenant_id"],
                "merchant_id": "unrelated-merchant",
                "identity": "unrelated",
            }
        ],
        [],
    )
    with graph_driver.session() as session:
        row = session.run(
            "MATCH(a:Merchant {tenant_id:$tenant})-[r:SHARED_IDENTITY]->(b) "
            "RETURN toString(r.occurred_at) AS when",
            tenant=query["tenant_id"],
        ).single()
        assert row["when"] == "2018-05-22T00:00:00Z"


@pytest.mark.neo4j_integration
@pytest.mark.parametrize("foreign_state", ["missing", "late_start", "early_end", "undeclared"])
def test_graph_shared_scope_is_declared_independently_of_history(graph_driver, foreign_state):
    query, rows, identities = records(graph_driver.test_tenants)
    rows = [r for r in rows if r["transaction"]["tenant_id"] == query["tenant_id"]]
    import_graph(graph_driver, rows, identities, coverage(graph_driver.test_tenants))
    graph = Neo4jEvidence(graph_driver)
    task = {"transaction": query}
    assert "neighbourhood" in graph.for_task(task, config())
    with graph_driver.session() as session:
        foreign = graph_driver.test_tenants[1]
        if foreign_state == "missing":
            session.run(
                "MATCH(c:EvidenceCoverage {tenant_id:$tenant}) DELETE c", tenant=foreign
            ).consume()
        elif foreign_state == "late_start":
            session.run(
                "MATCH(c:EvidenceCoverage {tenant_id:$tenant}) "
                "SET c.history_from=datetime('2018-05-01T00:00:00Z')",
                tenant=foreign,
            ).consume()
        elif foreign_state == "early_end":
            session.run(
                "MATCH(c:EvidenceCoverage {tenant_id:$tenant}) "
                "SET c.history_until=datetime('2018-06-01T11:00:00Z')",
                tenant=foreign,
            ).consume()
        else:
            session.run(
                "MATCH(m:MerchantIdentity) WHERE m.tenant_id IN $tenants DELETE m",
                tenants=list(graph_driver.test_tenants),
            ).consume()
    result = graph.for_task(task, config())
    assert any("shared-merchant scope" in x for x in result["coverage"]["missing"])
    assert "neighbourhood" not in result
    assert "graph_projection" not in result

"""Controlled retrieval distinguishes exact membership from vector relevance."""

import pytest


def test_benchmark_preserves_missing_graph_and_exact_candidate_disagreement():
    from reckoner.v1.benchmark.queries import benchmark_queries

    class Store:
        def __init__(self, graph=False):
            self.graph = graph

        def neighbourhood(self, case):
            return None if self.graph and case["transaction_id"] == "missing" else ["a", "b"]

        def candidates(self, case):
            return ["a", "c"] if self.graph else ["a", "b"]

    report = benchmark_queries(
        [{"transaction_id": "q"}, {"transaction_id": "missing"}], Store(), Store(True)
    )
    assert report["query_count"] == 2
    assert report["exact_neighbourhood_matches"] == 1
    assert report["uncovered_queries"] == ["missing"]
    assert report["exact_candidate_matches"] == 0
    assert report["queries"][0]["candidate_difference"] == {"sql_only": ["b"], "cypher_only": ["c"]}
    assert report["decision_impact"]["status"] == "unavailable"
    assert len(report["queries"][0]["timings"]["sql_ms"]) == 6
    assert report["cache_protocol"]["host_cold"] is False


def test_benchmark_rejects_duplicate_query_membership():
    from reckoner.v1.benchmark.queries import benchmark_queries

    with pytest.raises(ValueError, match="duplicate"):
        benchmark_queries([{"transaction_id": "q"}, {"transaction_id": "q"}], None, None)


def test_card_vectors_use_candidate_cutoff_and_full_previous_history():
    from evidence_fixtures import transaction
    from reckoner.v1.benchmark.queries import candidate_vectors
    from reckoner.v1.evidence.features import feature_vector
    from test_v1_retrieval import config

    q = {**transaction(), "transaction_id": "candidate", "occurred_at": "2018-06-01T12:00:00Z"}
    before = {**q, "transaction_id": "before", "occurred_at": "2018-05-31T12:00:00Z"}
    equal = {**q, "transaction_id": "equal"}
    future = {**q, "transaction_id": "future", "occurred_at": "2018-06-02T00:00:00Z"}

    class History:
        def card_before(self, tenant, card, cutoff, since=None):
            return iter([before, equal, future])

        def previous_card(self, tenant, card, cutoff):
            return None

    scaler = config()["scaler"]
    actual = list(candidate_vectors([q], History(), scaler))
    expected = feature_vector(
        q, {"status": "available", "transactions": [before], "previous": before}, scaler
    )
    assert actual == [(q, expected)]


@pytest.mark.integration
@pytest.mark.neo4j_integration
def test_real_benchmark_exact_sets_and_vector_top_five_are_separate(pg, graph_driver):
    from evidence_fixtures import coverage, records
    from reckoner.v1.benchmark.queries import CypherQueries, SQLQueries, benchmark_queries
    from reckoner.v1.evidence.neo4j import import_graph
    from reckoner.v1.evidence.postgres import import_evidence
    from test_v1_retrieval import config

    q, rows, identities = records(graph_driver.test_tenants)
    cfg = config()
    import_evidence(
        pg.owner_dsn, [q], rows, identities, coverage(graph_driver.test_tenants), cfg["scaler"]
    )
    import_graph(graph_driver, rows, identities, coverage(graph_driver.test_tenants))
    sql = SQLQueries(pg.runner_dsn, cfg["scaler"])
    graph = CypherQueries(graph_driver)
    report = benchmark_queries([q], sql, graph)
    assert report["exact_neighbourhood_matches"] == 1
    assert report["exact_candidate_matches"] == 1
    assert len(report["queries"][0]["sql_candidates"]) == 6
    assert len(sql.top_five(q)) == 5
    assert [r["transaction_id"] for r in sql.top_five(q)] == ["a", "b", "c", "d", "e"]
    assert (
        sql.top_five(q)[0]["distance"] > 0
    )  # Query has newer card history than the tied candidates.
    assert len({r["distance"] for r in sql.top_five(q)}) == 1
    sql.close()
    graph.close()


def test_report_refuses_overwrite_and_marks_unavailable_decision_impact(tmp_path):
    from reckoner.v1.benchmark.report import write_report

    body = {
        "schema_version": "reckoner-retrieval-benchmark-v1",
        "query_count": 0,
        "exact_neighbourhood_matches": 0,
        "exact_candidate_matches": 0,
        "uncovered_queries": [],
        "latency_ms": {},
        "decision_impact": {"status": "unavailable"},
        "dataset_simulated": True,
    }
    result = write_report(body, tmp_path / "report")
    assert (tmp_path / "report.json").exists()
    assert "unavailable" in (tmp_path / "report.md").read_text()
    assert result["report_id"]
    with pytest.raises(FileExistsError):
        write_report(body, tmp_path / "report")

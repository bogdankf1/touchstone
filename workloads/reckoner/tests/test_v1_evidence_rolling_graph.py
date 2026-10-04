"""Neo4j rolling graph on a fabricated simulated source, in a dedicated disposable store.

`RECKONER_TEST_EVIDENCE_NEO4J_URI` must name an EMPTY Neo4j/GDS store used only by these
tests: the rolling graph refuses stores carrying smoke/disposable markers, so it cannot
share the store that `graph_driver` marks. A missing service fails; nothing is skipped.
"""

import json
import os
import uuid
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from reckoner.contracts import content_id
from test_v1_evidence_preparation import SNAPSHOT, build_inputs, fabricated_scaler
from test_v1_evidence_rolling import _declare, _environment, _run, config, identities

pytestmark = [pytest.mark.neo4j_integration, pytest.mark.integration]
TENANTS = ("tenant-a", "tenant-b")


@pytest.fixture(scope="module")
def inputs(tmp_path_factory):
    return build_inputs(tmp_path_factory.mktemp("rolling-graph-inputs"))


@pytest.fixture(scope="module")
def manifest(inputs):
    from reckoner.v1.evidence.preparation import entity_manifest

    source, _, bundle = inputs
    return entity_manifest(bundle, source)


def wipe(driver):
    with driver.session() as session:
        session.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 5000 ROWS").consume()


def count(driver, query, **params):
    with driver.session() as session:
        return session.run(query, **params).single()[0]


@pytest.fixture
def evidence_graph():
    from neo4j import GraphDatabase

    uri = os.environ.get("RECKONER_TEST_EVIDENCE_NEO4J_URI")
    if not uri:
        pytest.fail("rolling-graph integration requires RECKONER_TEST_EVIDENCE_NEO4J_URI")
    password = os.environ.get("RECKONER_TEST_EVIDENCE_NEO4J_PASSWORD", "reckoner-test-only")
    driver = GraphDatabase.driver(uri, auth=("neo4j", password), warn_notification_severity="OFF")
    driver.verify_connectivity()
    if count(driver, "MATCH (n) RETURN count(n)"):
        driver.close()
        pytest.fail("rolling-graph tests need an empty dedicated Neo4j store")
    try:
        yield driver
    finally:
        wipe(driver)
        driver.close()


@pytest.fixture
def source(inputs):
    from reckoner.v1.data.history import SourceHistory
    from reckoner.v1.evidence.rolling import SourceDays

    path, _, bundle = inputs
    history = SourceHistory(bundle, path)
    days = SourceDays(history, bundle)
    yield days
    days.close()
    history.close()


def graph(driver, preparation="prep-a", purpose="evidence"):
    from reckoner.v1.evidence.rolling_graph import RollingGraph

    return RollingGraph(driver, preparation_id=preparation, snapshot_id=SNAPSHOT, purpose=purpose)


def roll(rolling, source, days):
    from reckoner.v1.evidence.preparation import window

    for day in days:
        rolling.advance_to(day, source)
        rolling.evict_before(window(day)[0])


def test_rolling_and_fresh_window_projections_are_equal(evidence_graph, source, manifest):
    rolling = graph(evidence_graph)
    rolling.seed(manifest)
    roll(rolling, source, ["2018-01-10", "2018-02-20", "2018-04-02"])
    cutoff = "2018-04-02T00:00:00Z"
    rolled = rolling.edge_summary(cutoff)
    rolled_receipt = rolling.projection_for("2018-04-02", referenced=lambda _: False)
    wipe(evidence_graph)
    fresh = graph(evidence_graph)
    fresh.seed(manifest)
    roll(fresh, source, ["2018-04-02"])
    assert fresh.coverage()["tenant-a"]["history_from"].to_native() == datetime(
        2017, 12, 26, tzinfo=UTC
    )
    assert fresh.edge_summary(cutoff) == rolled
    fresh_receipt = fresh.projection_for("2018-04-02", referenced=lambda _: False)
    for key in (
        "node_count",
        "edge_count",
        "covered_accounts",
        "covered_cards",
        "covered_merchants",
        "cutoff",
    ):
        assert fresh_receipt[key] == rolled_receipt[key], key
    assert rolled_receipt["node_count"] == rolled["node_count"]
    assert rolled_receipt["edge_count"] == rolled["directed_edge_count"]
    assert rolled["card_merchant_edges"] and rolled["shared_identity"] and rolled["owns"]


def test_seeded_links_survive_eviction_and_later_entities_stay_out_of_earlier_projections(
    evidence_graph, source, manifest
):
    rolling = graph(evidence_graph)
    rolling.seed(manifest)
    totals = "MATCH ()-[r:OWNS|SHARED_IDENTITY]->() RETURN count(r)"
    seeded = count(evidence_graph, totals)
    roll(rolling, source, ["2018-03-15"])
    early = "MATCH (t:Transaction) WHERE t.occurred_at < datetime('2018-05-25T00:00:00Z') "
    assert count(evidence_graph, early + "RETURN count(t)")
    roll(rolling, source, ["2018-08-30"])
    assert count(evidence_graph, totals) == seeded
    assert (
        count(
            evidence_graph,
            "MATCH (t:Transaction) WHERE t.occurred_at < datetime('2018-05-25T00:00:00Z') "
            "RETURN count(t)",
        )
        == 0
    )


def projected_degrees(driver, cutoff, keys):
    """Degrees in the ACTUAL GDS projection built by `project.cypher` (then dropped)."""
    from reckoner.v1.evidence.neo4j import CYPHER

    name = "r1-test-" + uuid.uuid4().hex
    with driver.session() as session:
        session.run(
            (CYPHER / "project.cypher").read_text(), name=name, cutoff=cutoff, tenants=list(TENANTS)
        ).consume()
        try:
            return {
                row["key"]: row["degree"]
                for row in session.run(
                    "CALL gds.degree.stream($name) YIELD nodeId, score "
                    "WITH gds.util.asNode(nodeId) AS n, score WHERE n.key IN $keys "
                    "RETURN n.key AS key, score AS degree",
                    name=name,
                    keys=keys,
                )
            }
        finally:
            session.run(
                "CALL gds.graph.drop($name) YIELD graphName RETURN graphName", name=name
            ).consume()


def test_later_entities_and_links_stay_out_of_earlier_gds_projections(
    evidence_graph, source, manifest
):
    """R1: seeded from full history, yet a projection admits only what preceded its cutoff."""
    rolling = graph(evidence_graph)
    rolling.seed(manifest)
    roll(rolling, source, ["2018-03-16"])
    (late,) = [
        e
        for e in manifest["entities"]
        if e["kind"] == "card" and e["first_observed_at"] == "2018-03-15T10:00:00Z"
    ]
    on_day = projected_degrees(evidence_graph, "2018-03-15T00:00:00Z", [late["key"]])
    next_day = projected_degrees(evidence_graph, "2018-03-16T00:00:00Z", [late["key"]])
    assert late["key"] not in on_day  # neither the card nor its ownership edge
    assert next_day[late["key"]] >= 1  # card present with at least its ownership edge
    # Merchant 2001 is first observed in one tenant on 2018-02-01 and in the other on
    # 2018-03-05T10:00Z, which is when their shared-identity link is first observed.
    shared = content_id({"dataset": "cctd", "entity": "merchant", "source_merchant": "2001"})
    early, later = sorted(
        (e for e in manifest["entities"] if e.get("shared_identity") == shared),
        key=lambda e: e["first_observed_at"],
    )
    assert early["tenant_id"] != later["tenant_id"]
    assert later["first_observed_at"] == "2018-03-05T10:00:00Z"
    before = projected_degrees(evidence_graph, "2018-03-05T00:00:00Z", [early["key"], later["key"]])
    after = projected_degrees(evidence_graph, "2018-03-06T00:00:00Z", [early["key"], later["key"]])
    # The early merchant's only transaction (2018-02-01) is outside both 30-day windows, so
    # its degree is the shared link alone: absent before the link, one edge after it.
    assert before == {early["key"]: 0.0}
    assert after == {early["key"]: 1.0, later["key"]: 2.0}


def test_metrics_are_released_after_the_day_and_receipts_are_kept(evidence_graph, source, manifest):
    rolling = graph(evidence_graph)
    rolling.seed(manifest)
    roll(rolling, source, ["2018-03-01"])
    receipt = rolling.projection_for("2018-03-01", referenced=lambda _: False)
    metrics = "MATCH (m:GDSMetric {projection_id:$id}) RETURN count(m)"
    assert count(evidence_graph, metrics, id=receipt["projection_id"]) == receipt["node_count"]
    assert rolling.release(receipt["projection_id"]) == receipt["node_count"]
    assert count(evidence_graph, metrics, id=receipt["projection_id"]) == 0
    assert (
        count(
            evidence_graph,
            "MATCH (p:ProjectionReceipt {projection_id:$id}) RETURN count(p)",
            id=receipt["projection_id"],
        )
        == 2
    )


def test_partial_receipts_are_rebuilt_only_when_unreferenced(evidence_graph, source, manifest):
    rolling = graph(evidence_graph)
    rolling.seed(manifest)
    roll(rolling, source, ["2018-03-01"])
    first = rolling.projection_for("2018-03-01", referenced=lambda _: False)
    again = rolling.projection_for("2018-03-01", referenced=lambda _: False)
    assert (again["action"], again["projection_id"]) == ("reuse", first["projection_id"])
    with evidence_graph.session() as session:
        session.run("MATCH (p:ProjectionReceipt {tenant_id:'tenant-b'}) DETACH DELETE p").consume()
    with pytest.raises(ValueError, match="referenced"):
        rolling.projection_for("2018-03-01", referenced=lambda _: True)
    rebuilt = rolling.projection_for("2018-03-01", referenced=lambda _: False)
    assert rebuilt["action"] == "rebuild" and rebuilt["projection_id"] != first["projection_id"]
    assert (
        count(evidence_graph, "MATCH (p:ProjectionReceipt) RETURN count(DISTINCT p.projection_id)")
        == 1
    )
    assert count(evidence_graph, "MATCH (m:GDSMetric) RETURN count(m)") == rebuilt["node_count"]


@pytest.mark.parametrize(
    "setup",
    [
        "CREATE (:DisposableStore {created_by:'reckoner-tests'})",
        "CREATE (:SmokeStore {source_id:'x'})",
        "CREATE (:Entity {key:'foreign', tenant_id:'tenant-a'})",
    ],
)
def test_rolling_graph_refuses_marked_or_foreign_stores(evidence_graph, manifest, setup):
    with evidence_graph.session() as session:
        session.run(setup).consume()
    with pytest.raises(ValueError, match="refus"):
        graph(evidence_graph).seed(manifest)
    assert count(evidence_graph, "MATCH (n) RETURN count(n)") == 1


def test_a_store_owned_by_another_preparation_or_purpose_is_refused(evidence_graph):
    graph(evidence_graph).claim()
    for other in (graph(evidence_graph, "prep-b"), graph(evidence_graph, purpose="graph-check")):
        with pytest.raises(ValueError, match="refus"):
            other.claim()
    assert graph(evidence_graph).claim() == "owned"


def _day_rows(source, day):
    start = datetime.fromisoformat(day).replace(tzinfo=UTC)
    return [tx for tx, _ in source.records(start, start + timedelta(days=1))]


def test_gds_evidence_from_persisted_relational_equals_live_postgres(
    evidence_graph, source, manifest, inputs, pg
):
    from reckoner.v1.evidence.assemble import assemble_evidence
    from reckoner.v1.evidence.neo4j import Neo4jEvidence
    from reckoner.v1.evidence.postgres import PostgresEvidence
    from reckoner.v1.evidence.preparation import (
        build_manifests,
        check_documents,
        first_observations,
        window,
    )
    from reckoner.v1.evidence.rolling import (
        PersistedRelational,
        RollingStore,
        drop_working_set,
        ensure_working_set,
        import_queries,
        persist_documents,
        persisted_summaries,
        runtime_dsn,
        with_database,
        working_set_name,
    )

    day = "2018-03-15"
    queries = _day_rows(source, day)
    late = next(tx for tx in queries if tx["occurred_at"] == "2018-03-15T10:00:00Z")
    preparation = uuid.uuid4().hex * 2
    owner = ensure_working_set(pg.owner_dsn, preparation)
    ws_runner = with_database(pg.runner_dsn, working_set_name(preparation))
    try:
        store = RollingStore(
            owner, ws_runner, source=source, scaler=fabricated_scaler(), snapshot_id=SNAPSHOT
        )
        store.initialize(identities(manifest))
        store.advance_to(day)
        store.evict_before(window(day)[0])
        store.import_previous_cards(queries)
        relational = store.evidence(queries, config())
        import_queries(pg.owner_dsn, queries)
        persist_documents(pg.runner_dsn, relational)
        cases = [
            {
                "tenant_id": tx["tenant_id"],
                "transaction_id": tx["transaction_id"],
                "population": "validation",
                "modes": ["relational", "gds-augmented"],
                "pilot": False,
                "transaction": tx,
            }
            for tx in queries
        ]
        declaration = {
            "preparation_id": "p" * 64,
            "source_snapshot_id": SNAPSHOT,
            "populations": {"validation": {"sample_id": "v" * 64}},
        }
        (relational_manifest,) = build_manifests(
            declaration, [{"day": day, "cases": cases}], relational, mode="relational"
        )
        rolling = graph(evidence_graph)
        rolling.seed(manifest)
        roll(rolling, source, [day])
        receipt = rolling.projection_for(day, referenced=lambda _: False)
        persisted = PersistedRelational(pg.runner_dsn, [relational_manifest])
        live = PostgresEvidence(runtime_dsn(ws_runner))
        evidence = Neo4jEvidence(evidence_graph)
        from_persisted = [
            assemble_evidence({"transaction": tx}, config(), persisted, evidence) for tx in queries
        ]
        from_live = [
            assemble_evidence({"transaction": tx}, config(), live, evidence) for tx in queries
        ]
        assert from_persisted == from_live
        summary = check_documents(
            day,
            [(c, "gds-augmented", d) for c, d in zip(cases, from_persisted, strict=True)],
            first_observed=first_observations(manifest),
        )
        assert summary["intrinsic"] == {"query card absent from GDS projection": 1}
        by_id = {d["transaction_id"]: d for d in from_persisted}
        assert by_id[late["transaction_id"]]["coverage"] == {
            "status": "partial",
            "missing": ["query card absent from GDS projection"],
        }
        assert all(
            d["graph_projection"]["projection_id"] == receipt["projection_id"]
            for d in from_persisted
        )
        persist_documents(pg.runner_dsn, from_persisted)
        modes = {
            "gds-augmented" if s["graph_projection"] else "relational"
            for s in persisted_summaries(pg.runner_dsn, SNAPSHOT)
        }
        assert modes == {"relational", "gds-augmented"}
    finally:
        drop_working_set(pg.owner_dsn, preparation)


GRAPH_DAYS = "2019-01-03"


def test_graph_pass_end_to_end_and_an_unreachable_graph_fails_the_day(
    evidence_graph, inputs, pg, tmp_path, monkeypatch
):
    from neo4j.exceptions import ServiceUnavailable
    from reckoner.v1.evidence import rolling, steps
    from reckoner.v1.evidence.neo4j import Neo4jEvidence
    from reckoner.v1.evidence.preparation import PreparationFault
    from reckoner.v1.evidence.rolling import drop_working_set

    _environment(monkeypatch, inputs, pg)
    monkeypatch.setenv("RECKONER_NEO4J_URI", os.environ["RECKONER_TEST_EVIDENCE_NEO4J_URI"])
    monkeypatch.setenv("RECKONER_NEO4J_USER", "neo4j")
    monkeypatch.setenv(
        "RECKONER_NEO4J_PASSWORD",
        os.environ.get("RECKONER_TEST_EVIDENCE_NEO4J_PASSWORD", "reckoner-test-only"),
    )
    source, _, bundle = inputs
    scaler = tmp_path / "scaler.json"
    scaler.write_text(json.dumps(fabricated_scaler()))
    declared = steps.run(
        Namespace(
            evidence_step="declare",
            bundle=bundle,
            scaler=scaler,
            output_root=tmp_path / "out",
            population=["cohort-2019=relational,gds-augmented"],
            env_file="-",
        )
    )
    declaration = tmp_path / "out" / declared["preparation_id"][:12] / "declaration.json"

    def graph_run(through):
        return steps.run(
            Namespace(
                evidence_step="run",
                declaration=declaration,
                evidence_pass="graph",
                through=through,
                env_file="-",
                free_floor_bytes=0,
                max_store_bytes=None,
                free_path=None,
            )
        )

    def publish(name):
        return steps.run(
            Namespace(
                evidence_step="publish",
                declaration=declaration,
                evidence_pass=name,
                population=None,
                env_file="-",
            )
        )

    try:
        with pytest.raises(ValueError, match="publish"):
            graph_run(GRAPH_DAYS)
        _run(declaration, through="2019-01-31")
        published = publish("relational")
        assert [(m["population"], m["cases"]) for m in published["manifests"]] == [
            ("cohort-2019", 1000)
        ]
        result = graph_run(GRAPH_DAYS)
        assert result["processed_days"] == 3 and result["seed"]["seeded"]
        assert result["runtime_identity"]["current_user"] == "reckoner_runner"
        assert result["runtime_identity"]["oracle_usage"] is False
        stage = result["stage"]
        assert stage["documents"] == sum(
            r["cases"]
            for r in [json.loads(line) for line in (declaration.parent / "days-graph.jsonl").open()]
        )
        assert stage["page_rank_non_converged"] + stage["page_rank_converged"] == stage["documents"]
        assert set(stage["snapshot_age_seconds"]) == {"min", "median", "max"}
        stages = [json.loads(line) for line in (declaration.parent / "stages.jsonl").open()]
        assert stages[-1]["mode"] == "gds-augmented" and stages[-1]["through"] == GRAPH_DAYS
        receipts = [json.loads(line) for line in (declaration.parent / "days-graph.jsonl").open()]
        assert [r["day"] for r in receipts] == ["2019-01-01", "2019-01-02", "2019-01-03"]
        assert all(r["released_metrics"] for r in receipts)
        assert count(evidence_graph, "MATCH (m:GDSMetric) RETURN count(m)") == 0
        projections = (declaration.parent / "projections.jsonl").read_text().splitlines()
        assert len(projections) == 3
        # Lost receipts are reconstructed from persisted facts and the kept graph receipts.
        expected = {json.loads(line)["projection_id"] for line in projections}
        (declaration.parent / "projections.jsonl").unlink()
        (declaration.parent / "days-graph.jsonl").unlink()
        again = graph_run(GRAPH_DAYS)
        assert again["complete_days"] == 3 and again["processed_days"] == 0
        rebuilt = [json.loads(line) for line in (declaration.parent / "projections.jsonl").open()]
        assert {r["projection_id"] for r in rebuilt} == expected
        assert all(r["receipt_reconstructed"] for r in rebuilt)
        with psycopg.connect(pg.owner_dsn) as connection:
            before = connection.execute("SELECT count(*) FROM reckoner.v1_evidence").fetchone()[0]

        def unreachable(self, task, config_):
            raise ServiceUnavailable("graph stopped")

        monkeypatch.setattr(Neo4jEvidence, "for_task", unreachable)
        # Assembly normally runs in a spawned child; run it inline so the patch applies.
        monkeypatch.setattr(rolling, "isolated", lambda function, *args: function(*args))
        with pytest.raises(PreparationFault) as raised:
            graph_run("2019-01-04")
        assert raised.value.failures and all(
            "graph unavailable: service unreachable" in f["reasons"] for f in raised.value.failures
        )
        with psycopg.connect(pg.owner_dsn) as connection:
            after = connection.execute("SELECT count(*) FROM reckoner.v1_evidence").fetchone()[0]
        assert after == before
        with pytest.raises(ValueError, match="missing"):
            publish("graph")
    finally:
        drop_working_set(pg.owner_dsn, declared["preparation_id"])


def test_graph_check_reconciles_against_a_bounded_canonical_import(
    evidence_graph, inputs, pg, source, tmp_path, monkeypatch
):
    from reckoner.v1.evidence import steps
    from reckoner.v1.evidence.preparation import window

    _environment(monkeypatch, inputs, pg)
    monkeypatch.setenv("RECKONER_NEO4J_URI", os.environ["RECKONER_TEST_EVIDENCE_NEO4J_URI"])
    monkeypatch.setenv("RECKONER_NEO4J_USER", "neo4j")
    monkeypatch.setenv(
        "RECKONER_NEO4J_PASSWORD",
        os.environ.get("RECKONER_TEST_EVIDENCE_NEO4J_PASSWORD", "reckoner-test-only"),
    )
    declaration, declared = _declare(inputs, tmp_path)
    day = "2018-04-02"
    start, end = window(day)
    records = tmp_path / "records.jsonl"
    with records.open("w") as handle:
        for tx, _ in source.records(start, end):
            handle.write(json.dumps({"transaction": tx}) + "\n")
    result = steps.run(
        Namespace(
            evidence_step="graph-check",
            declaration=declaration,
            day=day,
            reference_records=records,
            reference_receipt=None,
            env_file="-",
            free_floor_bytes=0,
            max_store_bytes=None,
            free_path=None,
        )
    )
    assert result["reconciliation"]["card_merchant_equal"] is True
    assert result["reconciliation"]["owns_delta"] >= 0
    assert result["projection"]["node_count"] == result["edge_summary"]["node_count"]
    assert result["released_metrics"] == result["projection"]["node_count"]
    assert (declaration.parent / f"graph-check-{day}.json").exists()
    assert content_id(result["edge_summary"]) and result["dataset_simulated"] is True


def _operators(plan):
    found = [plan["operatorType"]]
    for child in plan.get("children", []):
        found += _operators(child)
    return found


def test_seed_and_import_lookups_seek_the_unique_entity_key_index(evidence_graph, manifest):
    """At archive scale a label scan per row never finishes (124,930 merchants)."""
    from reckoner.v1.evidence.neo4j import CYPHER
    from reckoner.v1.evidence.rolling_graph import SEED_OWNS, SEED_SHARED

    graph(evidence_graph).seed(manifest)
    for query in (SEED_OWNS, SEED_SHARED, (CYPHER / "import.cypher").read_text()):
        with evidence_graph.session() as session:
            plan = session.run("EXPLAIN " + query, rows=[]).consume().plan
        operators = _operators(plan)
        assert not [o for o in operators if "LabelScan" in o or o.startswith("AllNodesScan")], (
            query,
            operators,
        )
        assert any("NodeUniqueIndexSeek" in o for o in operators), (query, operators)


def test_the_in_band_guard_runs_before_a_projection_is_written(evidence_graph, source, manifest):
    from reckoner.v1.benchmark.resources import ResourceGuardError
    from reckoner.v1.evidence.rolling_graph import RollingGraph

    calls = []

    def guard(connection=None):
        calls.append("guard")
        if len(calls) > 1:
            raise ResourceGuardError("free disk below floor")
        return {}

    rolling = RollingGraph(
        evidence_graph, preparation_id="prep-a", snapshot_id=SNAPSHOT, guard=lambda c=None: {}
    )
    rolling.seed(manifest)
    roll(rolling, source, ["2018-03-01"])
    rolling.guard = guard
    guard()
    with pytest.raises(ResourceGuardError):
        rolling.projection_for("2018-03-01", referenced=lambda _: False)
    assert count(evidence_graph, "MATCH (p:ProjectionReceipt) RETURN count(p)") == 0
    assert count(evidence_graph, "MATCH (m:GDSMetric) RETURN count(m)") == 0

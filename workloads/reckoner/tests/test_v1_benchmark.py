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


def test_candidate_agreement_compares_sets_and_reports_duplicates():
    from reckoner.v1.benchmark.queries import benchmark_queries

    class Store:
        def __init__(self, ids):
            self.ids = ids

        def neighbourhood(self, case):
            return []

        def candidates(self, case):
            return self.ids[case["transaction_id"]]

    sql = Store({"ordered": ["a", "b"], "duplicate": ["a", "a", "b"]})
    cypher = Store({"ordered": ["b", "a"], "duplicate": ["a", "b"]})
    report = benchmark_queries(
        [{"transaction_id": "ordered"}, {"transaction_id": "duplicate"}], sql, cypher
    )
    ordered, duplicate = report["queries"]
    assert ordered["exact_candidates"] is True
    assert ordered["candidate_duplicates"] == {"sql": [], "cypher": []}
    assert duplicate["candidate_difference"] == {"sql_only": [], "cypher_only": []}
    assert duplicate["candidate_duplicates"] == {"sql": ["a"], "cypher": []}
    assert duplicate["exact_candidates"] is False
    assert report["exact_candidate_matches"] == 1


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
        "measurement_mode": "synthetic-fixture",
    }
    result = write_report(body, tmp_path / "report")
    assert (tmp_path / "report.json").exists()
    assert "unavailable" in (tmp_path / "report.md").read_text()
    assert result["report_id"]
    with pytest.raises(FileExistsError):
        write_report(body, tmp_path / "report")
    (tmp_path / "other.md").write_text("existing")
    with pytest.raises(FileExistsError):
        write_report(body, tmp_path / "other")
    assert not (tmp_path / "other.json").exists()


def retrieval_body(mode):
    return {
        "schema_version": "reckoner-retrieval-benchmark-v1",
        "query_count": 1,
        "exact_neighbourhood_matches": 1,
        "exact_candidate_matches": 1,
        "decision_impact": {"status": "unavailable"},
        "dataset_simulated": True,
        **({"measurement_mode": mode} if mode else {}),
    }


def test_report_refuses_unlabelled_or_unknown_measurement_mode(tmp_path):
    from reckoner.v1.benchmark.report import write_report

    for mode in (None, "measured"):
        with pytest.raises(ValueError, match="measurement_mode"):
            write_report(retrieval_body(mode), tmp_path / "report")
    assert list(tmp_path.iterdir()) == []


def test_measured_and_synthetic_reports_have_distinct_labels(tmp_path):
    from reckoner.v1.benchmark.report import write_report

    write_report(retrieval_body("measured-local-retrieval"), tmp_path / "measured")
    write_report(retrieval_body("synthetic-fixture"), tmp_path / "synthetic")
    measured = (tmp_path / "measured.md").read_text()
    synthetic = (tmp_path / "synthetic.md").read_text()
    assert measured.startswith("# Measured simulated-data retrieval benchmark\n")
    assert synthetic.startswith("# Synthetic-fixture retrieval benchmark\n")
    assert "plumbing only" in synthetic and "plumbing only" not in measured
    assert "Synthetic" not in measured.splitlines()[0]


def test_failed_report_publication_leaves_no_partial_pair(tmp_path, monkeypatch):
    import os

    from reckoner.v1.benchmark import report

    real_link, calls = os.link, []

    def failing_link(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError("simulated failure")
        real_link(source, target)

    monkeypatch.setattr(report.os, "link", failing_link)
    with pytest.raises(OSError, match="simulated"):
        report.write_report(retrieval_body("synthetic-fixture"), tmp_path / "report")
    assert list(tmp_path.iterdir()) == []


# Tracked measurement harness: pure protocol, guard, observation and report assembly.

GIB = 1024**3


def test_frozen_queries_select_declared_date_and_reject_duplicates():
    import json

    from reckoner.v1.benchmark.harness import frozen_queries

    rows = [
        {"transaction_id": "q1", "occurred_at": "2018-06-01T01:00:00Z"},
        {"transaction_id": "other", "occurred_at": "2018-06-02T00:00:00Z", "note": "2018-06-01"},
        {"transaction_id": "q2", "occurred_at": "2018-06-01T23:59:59Z"},
    ]
    lines = [json.dumps(r) for r in rows]
    assert [q["transaction_id"] for q in frozen_queries(lines, "2018-06-01")] == ["q1", "q2"]
    with pytest.raises(ValueError, match="no frozen queries"):
        frozen_queries(lines, "2019-01-01")
    with pytest.raises(ValueError, match="duplicate"):
        frozen_queries([lines[0], lines[0]], "2018-06-01")


def test_resource_guard_defaults_and_records_store_provenance():
    from reckoner.v1.benchmark.harness import ResourceGuardError, parse_stores, resource_guard

    stores = parse_stores(["neo4j=100"], {"postgres": 50})
    assert stores == {
        "neo4j": {"bytes": 100, "source": "declared"},
        "postgres": {"bytes": 50, "source": "measured"},
    }
    record = resource_guard(free_bytes=16 * GIB, artifact_bytes=10, stores=stores)
    assert record["derived_bytes"] == 160
    assert record["free_floor_bytes"] == 15 * GIB
    assert record["derived_cap_bytes"] == 20 * GIB
    assert record["stores"]["neo4j"]["source"] == "declared"
    with pytest.raises(ResourceGuardError, match="free disk"):
        resource_guard(free_bytes=15 * GIB - 1, artifact_bytes=0, stores={})
    with pytest.raises(ResourceGuardError, match="derived data"):
        resource_guard(free_bytes=30 * GIB, artifact_bytes=20 * GIB, stores=stores)
    assert (
        resource_guard(free_bytes=2, artifact_bytes=0, stores={}, free_floor=1, derived_cap=1)[
            "free_floor_bytes"
        ]
        == 1
    )
    with pytest.raises(ValueError, match="NAME=BYTES"):
        parse_stores(["neo4j"], {})
    with pytest.raises(ValueError, match="twice"):
        parse_stores(["postgres=1"], {"postgres": 2})


def test_protocol_declaration_is_content_addressed_and_tamper_evident():
    from reckoner.contracts import content_id
    from reckoner.v1.benchmark.harness import check_protocol, protocol_declaration

    queries = [{"transaction_id": "q1"}, {"transaction_id": "q2"}]
    protocol = protocol_declaration(
        queries, {"q1": ["b", "a"], "q2": ["c", "a"]}, "scaler", {"free_bytes": 1}
    )
    assert protocol["candidate_union_count"] == 3
    assert protocol["candidate_union_hash"] == content_id(["a", "b", "c"])
    assert protocol["populations"][0] == {
        "query_id": "q1",
        "candidate_count": 2,
        "candidate_ids": ["b", "a"],
        "candidate_hash": content_id(["b", "a"]),
    }
    assert protocol["provider_calls"] == 0 and protocol["host_cold"] is False
    assert check_protocol(protocol) is protocol
    protocol["queries"] = queries[:1]
    with pytest.raises(ValueError, match="protocol"):
        check_protocol(protocol)


class FakeStore:
    def __init__(self, members, candidates, top=None):
        self.members, self.ids, self.top = members, candidates, top

    def neighbourhood(self, case):
        return self.members[case["transaction_id"]]

    def candidates(self, case):
        return self.ids[case["transaction_id"]]

    def top_five(self, case):
        return self.top[case["transaction_id"]]


def measured():
    from reckoner.v1.benchmark.harness import measure_observation, protocol_declaration

    queries = [{"transaction_id": "q1"}, {"transaction_id": "q2"}]
    protocol = protocol_declaration(queries, {"q1": ["a"], "q2": ["a", "b"]}, "scaler", {})
    top = {
        "q1": [{"transaction_id": "a", "verdict": "decline", "distance": 0.0}],
        "q2": [
            {"transaction_id": "a", "verdict": "decline", "distance": 0.1},
            {"transaction_id": "b", "verdict": "approve", "distance": 0.2},
        ],
    }
    sql = FakeStore({"q1": [{"t": "a"}], "q2": []}, {"q1": ["a"], "q2": ["a", "b"]}, top)
    graph = FakeStore({"q1": [{"t": "a"}], "q2": []}, {"q1": ["a"], "q2": ["b", "a"]})
    observation, memberships = measure_observation(
        protocol,
        sql,
        graph,
        exclusions=lambda case: {"q1": 1, "q2": 2}[case["transaction_id"]],
        resources=lambda: {"free_bytes": 1},
        first_pass_state="after both database processes restarted",
    )
    return protocol, observation, memberships


def test_measure_observation_is_labelled_runtime_only_and_splits_memberships():
    from reckoner.contracts import content_id

    protocol, observation, memberships = measured()
    assert observation["measurement_mode"] == "measured-local-retrieval"
    assert observation["dataset_simulated"] is True and observation["provider_calls"] == 0
    assert observation["protocol_id"] == protocol["protocol_id"]
    assert observation["query_ids"] == ["q1", "q2"]
    assert observation["exact_candidate_matches"] == 2
    assert observation["cache_protocol"]["first_pass"] == "after both database processes restarted"
    assert observation["unsupported_exclusions"] == [
        {"query_id": "q1", "count": 1},
        {"query_id": "q2", "count": 2},
    ]
    first = observation["queries"][0]
    assert "sql_members" not in first and first["sql_members_count"] == 1
    assert first["sql_members_hash"] == content_id([{"t": "a"}])
    assert memberships[0]["sql_members"] == [{"t": "a"}]
    vector = observation["vector_results"][1]
    assert vector["candidate_coverage_complete"] is True and len(vector["latency_ms"]) == 6
    assert "query_label" not in vector and "label_agreement" not in vector
    assert observation["vector_latency_ms"]["warm"]["population"] == 10


def test_measure_observation_rejects_changed_vector_results():
    from reckoner.v1.benchmark.harness import vector_observations

    class Unstable:
        calls = 0

        def top_five(self, case):
            self.calls += 1
            return [{"transaction_id": str(self.calls)}]

    with pytest.raises(ValueError, match="changed"):
        vector_observations([{"transaction_id": "q"}], Unstable())


def test_replacement_receipt_requires_identical_memberships():
    from reckoner.contracts import content_id
    from reckoner.v1.benchmark.harness import replacement_receipt

    protocol, observation, _ = measured()
    same = FakeStore({"q1": [{"t": "a"}], "q2": []}, {})
    receipt = replacement_receipt(protocol, observation, same, same, reason="contaminated")
    assert receipt["receipt_id"] == content_id(
        {k: v for k, v in receipt.items() if k != "receipt_id"}
    )
    assert [r["transaction_id"] for r in receipt["queries"]] == ["q1", "q2"]
    assert receipt["warm_repeated"] is False and receipt["host_cold"] is False
    changed = FakeStore({"q1": [{"t": "z"}], "q2": []}, {})
    with pytest.raises(ValueError, match="membership"):
        replacement_receipt(protocol, observation, changed, same, reason="contaminated")


def annotations(**extra):
    return {
        "schema_version": "retrieval-report-annotations-v1",
        "limitations": ["Bounded fixture."],
        "relevance_interpretation": "diagnostic only",
        **extra,
    }


def test_assemble_report_joins_labels_applies_replacement_and_resources():
    from reckoner.contracts import content_id
    from reckoner.v1.benchmark.harness import assemble_report, replacement_receipt

    protocol, observation, _ = measured()
    observation = {**observation, "report_id": content_id(observation)}
    same = FakeStore({"q1": [{"t": "a"}], "q2": []}, {})
    receipt = replacement_receipt(protocol, observation, same, same, reason="contaminated")
    report = assemble_report(
        observation,
        protocol,
        labels={"q1": "fraud", "q2": "legitimate"},
        annotations=annotations(
            first_pass_protocol="replacement after restart",
            resource_accounting_stage="after shutdown",
            plan_observation="plan note",
        ),
        replacement=receipt,
        resources={"schema_version": "phase3-disk-reconciliation-v1", "free_bytes": 9},
        cgroup={"pg": {"memory.peak": "1"}},
        samples={"s.jsonl": [{"Name": "pg", "MemUsage": "1.5KiB / 2GiB"}]},
        artifacts={"s.jsonl": {"sha256": "0" * 64, "bytes": 1, "rows": 1}},
    )
    assert report["source_report_id"] == observation["report_id"]
    assert report["replacement_receipt_id"] == receipt["receipt_id"]
    assert report["cache_protocol"]["first_pass"] == "replacement after restart"
    assert report["cache_protocol"]["original_first_pass_valid"] is False
    original = observation["latency_ms"]["sql"]["first_pass"]
    assert report["invalid_original_first_pass_ms"]["sql"] == original
    assert report["queries"][0]["timings"]["sql_ms"][0] == receipt["queries"][0]["sql_ms"]
    assert (
        report["queries"][0]["timings"]["sql_ms"][1:]
        == (observation["queries"][0]["timings"]["sql_ms"][1:])
    )
    assert [v["label_agreement"] for v in report["vector_results"]] == [1.0, 0.5]
    assert report["vector_relevance_proxy"] == {
        "empty_queries": 0,
        "unavailable_queries": 0,
        "returned_pairs": 3,
        "matching_pairs": 2,
        "fraction": 2 / 3,
        "fraud_query_returned": 1,
        "fraud_query_matches": 1,
        "legitimate_query_returned": 2,
        "legitimate_query_matches": 1,
        "interpretation": "diagnostic only",
    }
    assert report["resources"] == {
        "schema_version": "phase3-disk-reconciliation-v1",
        "free_bytes": 9,
        "accounting_stage": "after shutdown",
    }
    assert report["service_sample_maximum_bytes"] == {"s.jsonl": {"pg": 1536}}
    assert report["limitations"] == ["Bounded fixture."]
    assert report["plan_observation"] == "plan note"
    assert observation["latency_ms"]["sql"]["first_pass"] == original  # input untouched


@pytest.mark.parametrize(
    "case",
    ["tampered", "protocol", "label", "conflict", "annotation", "missing", "unlabelled"],
)
def test_assemble_report_fails_closed(case):
    from reckoner.contracts import content_id
    from reckoner.v1.benchmark.harness import assemble_report

    protocol, observation, _ = measured()
    observation = {**observation, "report_id": content_id(observation)}
    labels = {"q1": "fraud", "q2": "legitimate"}
    notes = annotations()
    replacement = None
    if case == "tampered":
        observation["query_count"] = 9
    if case == "protocol":
        protocol = {**protocol, "protocol_id": "0" * 64}
    if case == "label":
        labels.pop("q2")
    if case == "conflict":
        observation["vector_results"][0]["query_label"] = "legitimate"
        observation["report_id"] = content_id(
            {k: v for k, v in observation.items() if k != "report_id"}
        )
    if case == "annotation":
        notes["unexpected"] = "text"
    if case == "missing":
        replacement = {"schema_version": "retrieval-replacement-first-pass-v1"}
    if case == "unlabelled":
        observation.pop("measurement_mode")
        observation["report_id"] = content_id(
            {k: v for k, v in observation.items() if k != "report_id"}
        )
    with pytest.raises(ValueError):
        assemble_report(
            observation,
            protocol,
            labels=labels,
            annotations=notes,
            replacement=replacement,
            artifacts={},
        )


def test_docker_memory_and_artifact_records(tmp_path):
    import hashlib

    from reckoner.v1.benchmark.harness import artifact_record, memory_bytes, sample_maxima

    assert memory_bytes("2.056GiB / 4GiB") == 2207613190
    assert memory_bytes("184.2MiB / 2GiB") == 193147699
    assert memory_bytes("512B / 1GiB") == 512
    with pytest.raises(ValueError):
        memory_bytes("unknown")
    rows = [
        {"Name": "a", "MemUsage": "1MiB / 2GiB"},
        {"Name": "a", "MemUsage": "2MiB / 2GiB"},
        {"Name": "b", "MemUsage": "1KiB / 2GiB"},
    ]
    assert sample_maxima(rows) == {"a": 2 * 1024**2, "b": 1024}
    path = tmp_path / "rows.jsonl"
    path.write_bytes(b'{"a":1}\n{"a":2}\n')
    assert artifact_record(path) == {
        "sha256": hashlib.sha256(b'{"a":1}\n{"a":2}\n').hexdigest(),
        "bytes": 16,
        "rows": 2,
    }
    other = tmp_path / "plain.json"
    other.write_text("{}")
    assert "rows" not in artifact_record(other)


def test_benchmark_report_cli_assembles_fresh_labelled_report_offline(tmp_path):
    import json

    from reckoner.cli import main
    from reckoner.contracts import content_id

    protocol, observation, _ = measured()
    observation = {**observation, "report_id": content_id(observation)}
    files = {
        "protocol.json": protocol,
        "retrieval-report.json": observation,
        "annotations.json": annotations(),
    }
    for name, value in files.items():
        (tmp_path / name).write_text(json.dumps(value))
    (tmp_path / "labels.jsonl").write_text(
        '{"transaction_id":"q1","label":"fraud"}\n{"transaction_id":"q2","label":"legitimate"}\n'
    )
    (tmp_path / "extra.log").write_text("evidence")
    argv = [
        "v1",
        "benchmark",
        "report",
        "--protocol",
        str(tmp_path / "protocol.json"),
        "--observation",
        str(tmp_path / "retrieval-report.json"),
        "--oracle-labels",
        str(tmp_path / "labels.jsonl"),
        "--annotations",
        str(tmp_path / "annotations.json"),
        "--attach",
        str(tmp_path / "extra.log"),
        "--output",
        str(tmp_path / "out" / "benchmark-report"),
    ]
    assert main(argv) == 0
    report = json.loads((tmp_path / "out" / "benchmark-report.json").read_text())
    assert report["measurement_mode"] == "measured-local-retrieval"
    assert sorted(report["artifacts"]) == ["extra.log", "protocol.json", "retrieval-report.json"]
    markdown = (tmp_path / "out" / "benchmark-report.md").read_text()
    assert markdown.startswith("# Measured simulated-data retrieval benchmark")
    assert main(argv) == 2  # never overwrites an existing report


@pytest.mark.parametrize("step", ["inventory", "prepare", "verify", "measure", "replacement"])
def test_benchmark_store_steps_require_named_environment_variables(
    step, monkeypatch, tmp_path, capsys
):
    from reckoner.cli import main

    for name in ("TASK11_TEST_DSN", "TASK11_TEST_USER", "TASK11_TEST_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    common = ["--output-dir", str(tmp_path), "--scaler", str(tmp_path / "scaler.json")]
    common += ["--pg-dsn-env", "TASK11_TEST_DSN"]
    extra = {
        "inventory": ["--artifact-root", str(tmp_path), "--bundle", str(tmp_path)],
        "prepare": ["--artifact-root", str(tmp_path), "--bundle", str(tmp_path)],
        "verify": ["--artifact-root", str(tmp_path)],
        "measure": ["--artifact-root", str(tmp_path), "--first-pass-state", "restarted"],
        "replacement": ["--reason", "contaminated"],
    }[step]
    extra += {
        "inventory": ["--query-date", "2018-06-01"],
        "prepare": ["--source-dir", str(tmp_path)],
    }.get(step, [])
    if step in ("measure", "replacement"):
        extra += ["--neo4j-uri", "bolt://127.0.0.1:1", "--neo4j-user-env", "TASK11_TEST_USER"]
        extra += ["--neo4j-password-env", "TASK11_TEST_PASSWORD"]
    assert main(["v1", "benchmark", step, *common, *extra]) == 2
    assert "environment variable TASK11_TEST_DSN is not set" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_disk_reconciliation_counts_artifacts_and_every_volume():
    from reckoner.v1.benchmark.harness import ResourceGuardError, disk_reconciliation

    containers = [{"name": "/c", "state": "exited", "oom_killed": False, "restart_count": 0}]
    record = disk_reconciliation(
        artifact_bytes=5,
        volume_bytes={"v1": 2, "v2": 3},
        free_bytes=16 * GIB,
        containers=containers,
    )
    assert record["total_derived_bytes"] == 10
    assert record["containers"] == containers
    with pytest.raises(ResourceGuardError):
        disk_reconciliation(
            artifact_bytes=20 * GIB + 1, volume_bytes={}, free_bytes=16 * GIB, containers=[]
        )
    assert parse_du_rows(["8 /audit/1", "4 /audit/0"], ["a", "b"]) == {"a": 4096, "b": 8192}


def parse_du_rows(rows, volumes):
    from reckoner.v1.benchmark.harness import parse_du

    return parse_du(rows, volumes)

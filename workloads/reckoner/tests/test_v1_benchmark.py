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

    from reckoner.v1.benchmark.protocol import frozen_queries

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
    from reckoner.v1.benchmark.resources import ResourceGuardError, parse_stores, resource_guard

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
    from reckoner.v1.benchmark.protocol import check_protocol, protocol_declaration

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


RUNTIME = {"current_user": "reckoner_runner", "session_user": "owner", "oracle_usage": False}


def measured():
    from reckoner.v1.benchmark.protocol import measure_observation, protocol_declaration

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
        runtime=RUNTIME,
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
    assert observation["runtime_identity"] == RUNTIME


def test_measure_observation_rejects_changed_vector_results():
    from reckoner.v1.benchmark.protocol import vector_observations

    class Unstable:
        calls = 0

        def top_five(self, case):
            self.calls += 1
            return [{"transaction_id": str(self.calls)}]

    with pytest.raises(ValueError, match="changed"):
        vector_observations([{"transaction_id": "q"}], Unstable())


def test_replacement_receipt_requires_identical_memberships():
    from reckoner.contracts import content_id
    from reckoner.v1.benchmark.protocol import replacement_receipt

    protocol, observation, _ = measured()
    same = FakeStore({"q1": [{"t": "a"}], "q2": []}, {})
    receipt = replacement_receipt(
        protocol, observation, same, same, reason="contaminated", runtime=RUNTIME
    )
    assert receipt["runtime_identity"] == RUNTIME
    assert receipt["receipt_id"] == content_id(
        {k: v for k, v in receipt.items() if k != "receipt_id"}
    )
    assert [r["transaction_id"] for r in receipt["queries"]] == ["q1", "q2"]
    assert receipt["warm_repeated"] is False and receipt["host_cold"] is False
    changed = FakeStore({"q1": [{"t": "z"}], "q2": []}, {})
    with pytest.raises(ValueError, match="membership"):
        replacement_receipt(
            protocol, observation, changed, same, reason="contaminated", runtime=RUNTIME
        )


def annotations(**extra):
    return {
        "schema_version": "retrieval-report-annotations-v1",
        "limitations": ["Bounded fixture."],
        "relevance_interpretation": "diagnostic only",
        **extra,
    }


def test_assemble_report_joins_labels_applies_replacement_and_resources():
    from reckoner.contracts import content_id
    from reckoner.v1.benchmark.assembly import assemble_report
    from reckoner.v1.benchmark.protocol import replacement_receipt

    protocol, observation, _ = measured()
    observation = {**observation, "report_id": content_id(observation)}
    same = FakeStore({"q1": [{"t": "a"}], "q2": []}, {})
    receipt = replacement_receipt(
        protocol, observation, same, same, reason="contaminated", runtime=RUNTIME
    )
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
    from reckoner.v1.benchmark.assembly import assemble_report

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

    from reckoner.v1.benchmark.assembly import artifact_record
    from reckoner.v1.benchmark.resources import memory_bytes, sample_maxima

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


def step_argv(step, tmp_path):
    common = ["--output-dir", str(tmp_path), "--scaler", str(tmp_path / "scaler.json")]
    common += ["--pg-dsn-env", "TASK11_TEST_DSN", "--artifact-root", str(tmp_path)]
    extra = {
        "inventory": ["--bundle", str(tmp_path), "--query-date", "2018-06-01"],
        "prepare": ["--bundle", str(tmp_path), "--source-dir", str(tmp_path)],
        "verify": [],
        "measure": ["--first-pass-state", "restarted"],
        "replacement": ["--reason", "contaminated"],
    }[step]
    if step in ("measure", "replacement"):
        extra += ["--neo4j-uri", "bolt://127.0.0.1:1", "--neo4j-user-env", "TASK11_TEST_USER"]
        extra += ["--neo4j-password-env", "TASK11_TEST_PASSWORD"]
    return ["v1", "benchmark", step, *common, *extra]


STORE_STEPS = ["inventory", "prepare", "verify", "measure", "replacement"]
STEP_OUTPUTS = {
    "inventory": ["candidates.jsonl", "protocol.json", "sql-function-plan.json"],
    "prepare": ["vectors.jsonl", "preparation.json"],
    "verify": ["stored-verification.json"],
    "measure": [
        "service-samples.jsonl",
        "exact-memberships.json",
        "retrieval-report.json",
        "retrieval-report.md",
    ],
    "replacement": ["replacement-service-samples.jsonl", "replacement-first-pass.json"],
}


@pytest.mark.parametrize("step", STORE_STEPS)
def test_benchmark_store_steps_require_named_environment_variables(
    step, monkeypatch, tmp_path, capsys
):
    from reckoner.cli import main

    for name in ("TASK11_TEST_DSN", "TASK11_TEST_USER", "TASK11_TEST_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert main(step_argv(step, tmp_path)) == 2
    assert "environment variable TASK11_TEST_DSN is not set" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def step_inputs(step, tmp_path):
    """Minimal valid inputs so a step reaches its pre-flight checks."""
    import json

    from reckoner.v1.benchmark.protocol import protocol_declaration

    query = {"transaction_id": "q1", "tenant_id": "t", "occurred_at": "2018-06-01T00:00:00Z"}
    (tmp_path / "scaler.json").write_text(json.dumps({"scaler_id": "s"}))
    (tmp_path / "runtime_validation.jsonl").write_text(json.dumps(query) + "\n")
    if step != "inventory":
        protocol = protocol_declaration([query], {"q1": ["a"]}, "s", {})
        (tmp_path / "protocol.json").write_text(json.dumps(protocol))
        (tmp_path / "candidates.jsonl").write_text(
            json.dumps({"tenant_id": "t", "transaction_id": "a"}) + "\n"
        )
        (tmp_path / "retrieval-report.json").write_text(json.dumps({"queries": []}))


def no_store_connections(monkeypatch):
    import neo4j
    import psycopg

    def refuse(*args, **kwargs):
        raise AssertionError("store contacted before pre-flight checks")

    monkeypatch.setattr(psycopg, "connect", refuse)
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", refuse)
    for name in ("TASK11_TEST_DSN", "TASK11_TEST_USER", "TASK11_TEST_PASSWORD"):
        monkeypatch.setenv(name, "fabricated-test-value")


@pytest.mark.parametrize(
    "step,output", [(step, output) for step in STORE_STEPS for output in STEP_OUTPUTS[step]]
)
def test_store_steps_refuse_any_existing_output_before_store_work(
    step, output, monkeypatch, tmp_path, capsys
):
    from reckoner.cli import main

    no_store_connections(monkeypatch)
    step_inputs(step, tmp_path)
    if step == "measure" and output.startswith("retrieval-report"):
        (tmp_path / "retrieval-report.json").unlink()
    (tmp_path / output).write_text("existing evidence")
    before = sorted(p.name for p in tmp_path.iterdir())
    assert main(step_argv(step, tmp_path)) == 2
    assert output in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert (tmp_path / output).read_text() == "existing evidence"


@pytest.mark.parametrize("step", STORE_STEPS)
def test_store_steps_run_resource_guard_before_store_work(step, monkeypatch, tmp_path, capsys):
    from reckoner.cli import main
    from reckoner.v1.benchmark import steps
    from reckoner.v1.benchmark.resources import ResourceGuardError

    no_store_connections(monkeypatch)
    step_inputs(step, tmp_path)
    if step == "measure":
        (tmp_path / "retrieval-report.json").unlink()

    class Refusing:
        def __init__(self, args):
            pass

        def __call__(self, connection=None, volumes=True):
            raise ResourceGuardError("free disk below floor: fabricated")

    monkeypatch.setattr(steps, "Guard", Refusing)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert main(step_argv(step, tmp_path)) == 2
    assert "free disk below floor" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_sampler_failure_is_raised_not_silently_lost(monkeypatch, tmp_path):
    import subprocess

    from reckoner.v1.benchmark import resources

    def failing(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "docker stats")

    monkeypatch.setattr(resources.subprocess, "check_output", failing)
    with pytest.raises(RuntimeError, match="service sampling failed"):
        with resources.Sampler(tmp_path / "samples.jsonl", ["container"], 0.01):
            pass


def test_runtime_conninfo_sets_role_and_keeps_existing_options():
    from psycopg.conninfo import conninfo_to_dict
    from reckoner.v1.benchmark.steps import runtime_conninfo

    plain = conninfo_to_dict(runtime_conninfo("host=h dbname=d user=u", "reckoner_runner"))
    assert plain["options"] == "-c role=reckoner_runner"
    assert plain["user"] == "u" and plain["dbname"] == "d"
    kept = conninfo_to_dict(
        runtime_conninfo("host=h options='-c statement_timeout=5'", "reckoner_runner")
    )
    assert kept["options"] == "-c statement_timeout=5 -c role=reckoner_runner"
    with pytest.raises(ValueError, match="role"):
        runtime_conninfo("host=h", "bad role; drop")


@pytest.mark.parametrize(
    "identity,message",
    [
        ({"current_user": "reckoner_runner", "oracle_usage": True}, "oracle"),
        ({"current_user": "postgres", "oracle_usage": False}, "runtime role"),
    ],
)
def test_runtime_identity_fails_closed(identity, message):
    from reckoner.v1.benchmark.steps import check_runtime_identity

    with pytest.raises(ValueError, match=message):
        check_runtime_identity({"session_user": "owner", **identity}, "reckoner_runner")
    allowed = {"current_user": "reckoner_runner", "session_user": "o", "oracle_usage": False}
    assert check_runtime_identity(allowed, "reckoner_runner") == allowed


def test_store_construction_failure_closes_neo4j_driver(monkeypatch):
    from argparse import Namespace

    import neo4j
    from reckoner.v1.benchmark import queries, steps

    class Driver:
        closed = False

        def verify_connectivity(self):
            pass

        def close(self):
            self.closed = True

    driver = Driver()
    monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda *a, **k: driver)

    def broken(*args, **kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr(queries, "SQLQueries", broken)
    args = Namespace(neo4j_uri="bolt://127.0.0.1:1", runtime_role="reckoner_runner")
    with pytest.raises(OSError):
        steps._stores(args, {"scaler_id": "s"}, ("host=h", ("u", "p")))
    assert driver.closed


def test_sql_queries_close_connection_when_setup_fails(monkeypatch):
    import psycopg
    from reckoner.v1.benchmark.queries import SQLQueries

    class Connection:
        closed = False

        def execute(self, *args):
            raise psycopg.OperationalError("setup failed")

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: connection)
    with pytest.raises(psycopg.OperationalError):
        SQLQueries("host=h", {"scaler_id": "s"})
    assert connection.closed


def test_guard_rejects_malformed_store_volume():
    from argparse import Namespace

    from reckoner.v1.benchmark.resources import Guard

    with pytest.raises(ValueError, match="NAME=VOLUME"):
        Guard(Namespace(store_volume=["neo4j"], du_image="image"))


def test_cypher_query_text_is_loaded_before_the_timed_region(monkeypatch):
    import json
    from pathlib import Path

    from reckoner.v1.benchmark.queries import CypherQueries
    from reckoner.v1.evidence.neo4j import CYPHER

    class Result(list):
        def single(self):
            return self[0] if self else None

    class Session:
        def __init__(self):
            self.queries = []

        def run(self, query, **params):
            self.queries.append(query)
            if "RETURN m.shared_identity" in query:
                return Result([{"identity": "m"}])
            if "EvidenceCoverage" in query:
                start, end = "2018-01-01T00:00:00Z", "2018-07-01T00:00:00Z"
                return Result([{"tenant": "t", "start": start, "end": end}])
            return Result([{"document": json.dumps({"x": 1}), "merchant_identity": "m"}])

        def close(self):
            pass

    session = Session()

    class Driver:
        def session(self, **kwargs):
            return session

    graph = CypherQueries(Driver())

    def no_reads(self, *args, **kwargs):
        raise AssertionError("file read inside the timed region")

    monkeypatch.setattr(Path, "read_text", no_reads)
    tx = {"tenant_id": "t", "merchant_id": "x", "card_id": "c"}
    tx["occurred_at"] = "2018-06-01T00:00:00Z"
    assert graph.neighbourhood(tx) == [{"transaction": {"x": 1}, "merchant_identity": "m"}]
    monkeypatch.undo()
    assert session.queries[-1] == (CYPHER / "neighbourhood.cypher").read_text()


def test_inventory_population_sorts_in_python_and_keys_by_tenant():
    from reckoner.v1.benchmark.protocol import inventory_population

    rows = {
        "q1": [{"tenant_id": "t", "transaction_id": i} for i in ("b", "a", "B")],
        "q2": [{"tenant_id": "t", "transaction_id": "a"}],
    }
    candidate_ids, members = inventory_population(rows)
    assert candidate_ids == {"q1": ["B", "a", "b"], "q2": ["a"]}
    assert sorted(members) == [("t", "B"), ("t", "a"), ("t", "b")]
    rows["q2"] = [{"tenant_id": "other", "transaction_id": "a"}]
    with pytest.raises(ValueError, match="two tenants"):
        inventory_population(rows)


def test_stored_verification_sorts_in_python_before_hashing():
    from reckoner.v1.benchmark.protocol import check_stored, protocol_declaration

    protocol = protocol_declaration([{"transaction_id": "q1"}], {"q1": ["B", "a", "b"]}, "s", {})
    coverage = [{"query_id": "q1", "complete": True}]
    result = check_stored(["b", "a", "B"], coverage, protocol)
    assert result["stored_count"] == 3
    with pytest.raises(ValueError, match="coverage"):
        check_stored(["b", "a", "B"], [{"query_id": "q1", "complete": False}], protocol)
    with pytest.raises(ValueError, match="union"):
        check_stored(["a", "b"], coverage, protocol)


def test_vectors_copy_explicit_columns_single_commit_and_bounded_guard():
    import io

    from reckoner.v1.benchmark.steps import write_vectors

    class Copy:
        def __init__(self, sink):
            self.sink = sink

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def write_row(self, row):
            self.sink.append(row)

    class Cursor:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def copy(self, statement):
            self.connection.statements.append(statement)
            return Copy(self.connection.rows)

    class Connection:
        def __init__(self):
            self.statements, self.rows, self.commits = [], [], 0

        def cursor(self):
            return Cursor(self)

        def commit(self):
            self.commits += 1

    connection, guards = Connection(), []
    pairs = [({"tenant_id": "t", "transaction_id": str(i)}, [0.0]) for i in range(5)]
    count, samples = write_vectors(
        pairs,
        io.StringIO(),
        connection,
        scaler_id="s",
        batch_size=2,
        guard=lambda c: guards.append(c) or {"n": len(guards)},
        guard_every=2,
    )
    assert count == 5 and len(connection.rows) == 5
    assert connection.commits == 1
    assert set(connection.statements) == {
        "COPY reckoner.v1_vectors (tenant_id, transaction_id, scaler_id, feature_version, "
        "features) FROM STDIN"
    }
    assert samples == [{"n": 1}, {"n": 2}, {"n": 3}]  # start, every second batch, end


def test_protocol_declaration_records_candidate_policy_used_by_inventory():
    from reckoner.v1.benchmark.protocol import (
        CANDIDATE_POLICY,
        inventory_parameters,
        protocol_declaration,
    )

    protocol = protocol_declaration([{"transaction_id": "q"}], {"q": []}, "s", {})
    assert protocol["candidate_policy"] == CANDIDATE_POLICY
    assert CANDIDATE_POLICY["resolution_policy_version"] == "simulated-seven-days-v1"
    assert CANDIDATE_POLICY["resolution_window_days"] == 90
    query = {"tenant_id": "t", "occurred_at": "2018-06-01T00:00:00Z"}
    assert inventory_parameters(query) == (
        "t",
        "simulated-seven-days-v1",
        "2018-06-01T00:00:00Z",
        "2018-06-01T00:00:00Z",
        90,
        "2018-06-01T00:00:00Z",
    )


def test_tracked_annotations_restate_no_unchecked_numbers():
    import json
    import re
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "docs/evidence/phase-3-benchmark-annotations.json"
    notes = json.loads(path.read_text())
    texts = [*notes["limitations"]]
    texts += [notes[k] for k in notes if k not in ("schema_version", "limitations")]
    assert texts and not [t for t in texts if re.search(r"\d", t)]
    assert not [t for t in texts if re.search(r"[a-z][0-9]|[0-9][a-z]|\w/\w", t)]


def test_disk_reconciliation_counts_artifacts_and_every_volume():
    from reckoner.v1.benchmark.resources import ResourceGuardError, disk_reconciliation

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
    from reckoner.v1.benchmark.resources import parse_du

    return parse_du(rows, volumes)


def test_report_prefix_with_dots_keeps_the_whole_name(tmp_path):
    from reckoner.v1.benchmark.report import write_report

    write_report(retrieval_body("synthetic-fixture"), tmp_path / "benchmark.v2")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["benchmark.v2.json", "benchmark.v2.md"]


def test_markdown_fence_cannot_be_closed_by_report_content(tmp_path):
    from reckoner.v1.benchmark.report import write_report

    body = {**retrieval_body("synthetic-fixture"), "note": "```\n# injected heading"}
    write_report(body, tmp_path / "report")
    markdown = (tmp_path / "report.md").read_text()
    assert "\n````json\n" in markdown and markdown.rstrip().endswith("````")


class PermissiveGuard:
    def __init__(self, args):
        pass

    def __call__(self, connection=None):
        return {"free_bytes": 1}


def test_sampler_failure_keeps_the_finished_one_shot_observation(monkeypatch, tmp_path, capsys):
    import json
    import subprocess

    from reckoner.cli import main
    from reckoner.v1.benchmark import resources, steps

    step_inputs("measure", tmp_path)
    (tmp_path / "retrieval-report.json").unlink()
    for name in ("TASK11_TEST_DSN", "TASK11_TEST_USER", "TASK11_TEST_PASSWORD"):
        monkeypatch.setenv(name, "fabricated-test-value")

    class Closable:
        connection = None

        def close(self):
            pass

    monkeypatch.setattr(steps, "Guard", PermissiveGuard)
    monkeypatch.setattr(
        steps, "_stores", lambda *a: (Closable(), Closable(), Closable(), dict(RUNTIME))
    )
    observation = {
        "measurement_mode": "measured-local-retrieval",
        "dataset_simulated": True,
        "queries": [],
        "latency_ms": {},
    }
    monkeypatch.setattr(steps, "measure_observation", lambda *a, **k: (dict(observation), []))

    def failing(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "docker stats")

    monkeypatch.setattr(resources.subprocess, "check_output", failing)
    argv = step_argv("measure", tmp_path) + ["--sample-container", "c", "--interval", "0.01"]
    assert main(argv) == 2
    assert "service sampling failed" in capsys.readouterr().err
    written = json.loads((tmp_path / "retrieval-report.json").read_text())
    assert written["resource_samples_complete"] is False
    assert (tmp_path / "exact-memberships.json").exists()


def test_store_runtime_identity_records_the_neo4j_user(monkeypatch):
    from argparse import Namespace

    import neo4j
    from reckoner.v1.benchmark import queries, steps

    class Result:
        def single(self):
            return {"username": "neo4j"}

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def run(self, statement, **kwargs):
            return Result()

    class Driver:
        def verify_connectivity(self):
            pass

        def session(self, **kwargs):
            return Session()

        def close(self):
            pass

    class SQL:
        connection = None

        def __init__(self, *args):
            pass

        def close(self):
            pass

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda *a, **k: Driver())
    monkeypatch.setattr(queries, "SQLQueries", SQL)
    monkeypatch.setattr(queries, "CypherQueries", lambda driver: object())
    monkeypatch.setattr(steps, "runtime_identity", lambda connection: dict(RUNTIME))
    args = Namespace(neo4j_uri="bolt://127.0.0.1:1", runtime_role="reckoner_runner")
    *_, identity = steps._stores(args, {"scaler_id": "s"}, ("host=h", ("neo4j", "p")))
    assert identity == {**RUNTIME, "neo4j_user": "neo4j"}


@pytest.mark.parametrize("name", ["report.json", "report.md", "report.JSON"])
def test_report_output_prefix_with_a_file_extension_is_rejected(tmp_path, name):
    from reckoner.v1.benchmark.report import write_report

    with pytest.raises(ValueError, match="prefix"):
        write_report(retrieval_body("synthetic-fixture"), tmp_path / name)
    assert list(tmp_path.iterdir()) == []


def test_vector_copy_cursor_is_closed():
    import io

    from reckoner.v1.benchmark.steps import write_vectors

    class Copy:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def write_row(self, row):
            pass

    class Cursor:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

        def copy(self, statement):
            return Copy()

        def close(self):
            self.closed = True

    class Connection:
        def __init__(self):
            self.cursors = []

        def cursor(self):
            self.cursors.append(Cursor())
            return self.cursors[-1]

        def commit(self):
            pass

    connection = Connection()
    pairs = [({"tenant_id": "t", "transaction_id": str(i)}, [0.0]) for i in range(3)]
    write_vectors(
        pairs,
        io.StringIO(),
        connection,
        scaler_id="s",
        batch_size=2,
        guard=lambda c: {},
        guard_every=1,
    )
    assert len(connection.cursors) == 2 and all(c.closed for c in connection.cursors)

"""`reckoner v1 benchmark` store steps executed end to end against disposable real stores.

Fabricated simulated source only. Requires the disposable Postgres (`RECKONER_TEST_OWNER_DSN`),
Neo4j/GDS (`RECKONER_TEST_NEO4J_URI`) and Docker names for the sampler and audit steps.
"""

import json
import os

import psycopg
import pytest
from reckoner.contracts import content_id
from test_v1_preparation import inputs  # noqa: F401  (fixture)

pytestmark = [pytest.mark.integration, pytest.mark.neo4j_integration]

TENANTS = ("tenant-a", "tenant-b")


def _required(name):
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"benchmark store integration requires {name}")
    return value


def _clear_graph(driver):
    from reckoner.v1.smoke import require_owned_graph

    require_owned_graph(driver)  # tenant-a/b are real archive tenants: require a marked store
    with driver.session() as session:
        session.run(
            "MATCH(n) WHERE n.tenant_id IN $tenants DETACH DELETE n", tenants=list(TENANTS)
        ).consume()


def _declare_scope(pg, driver, index):
    """Owner declarations the runtime scope checks need, mirrored in both stores."""
    from reckoner.v1.evidence.neo4j import import_graph

    with psycopg.connect(pg.owner_dsn) as connection:
        records = [
            {"transaction": history, "resolution": resolution}
            for history, resolution in connection.execute(
                "SELECT h.document, r.document FROM reckoner.v1_history h "
                "JOIN oracle.v1_resolutions r USING (tenant_id, transaction_id)"
            )
        ]
        identities = sorted(
            {(r["transaction"]["tenant_id"], r["transaction"]["merchant_id"]) for r in records}
        )
        identities = [{"tenant_id": t, "merchant_id": m, "identity": m} for t, m in identities]
        coverage = [
            {
                "tenant_id": tenant,
                "history_from": "2015-01-01T00:00:00Z",
                "history_until": "2020-01-01T00:00:00Z",
                "previous_card_complete": True,
                "source_snapshot_id": index["source_sha256"],
            }
            for tenant in TENANTS
        ]
        for item in identities:
            connection.execute(
                "INSERT INTO reckoner.v1_merchant_identities VALUES (%s,%s,%s)",
                (item["tenant_id"], item["merchant_id"], item["identity"]),
            )
        for c in coverage:
            connection.execute(
                "INSERT INTO reckoner.v1_evidence_coverage VALUES (%s,%s,%s,%s,%s)",
                tuple(c.values()),
            )
    import_graph(driver, records, identities, coverage)


def _cli(*argv):
    from reckoner.cli import main

    return main(["v1", "benchmark", *map(str, argv)])


def test_benchmark_store_steps_run_end_to_end_on_real_stores(
    inputs,  # noqa: F811
    pg,
    graph_driver,
    tmp_path,
    monkeypatch,
    capsys,
):
    from reckoner.v1.data.prepare import import_v1, prepare_v1
    from reckoner.v1.evidence.features import fit_scaler
    from test_v1_features import observation

    samples = _required("RECKONER_TEST_SAMPLE_CONTAINERS").split(",")
    audit_prefix = _required("RECKONER_TEST_AUDIT_PREFIX")
    du_image = _required("RECKONER_TEST_DU_IMAGE")
    source, baseline = inputs
    bundle = tmp_path / "prepared"
    index = prepare_v1(source, baseline, bundle)
    import_v1(bundle, source, pg.owner_dsn)
    _clear_graph(graph_driver)
    _declare_scope(pg, graph_driver, index)
    scaler = fit_scaler([observation(1), observation(9), observation(40)])
    (tmp_path / "scaler.json").write_text(json.dumps(scaler))
    validation = [
        json.loads(line) for line in (bundle / "runtime_validation.jsonl").read_text().splitlines()
    ]
    query_date = max(tx["occurred_at"] for tx in validation)[:10]  # earliest has no history
    monkeypatch.setenv("T12_OWNER_DSN", pg.owner_dsn)
    monkeypatch.setenv("T12_RUNNER_DSN", pg.runner_dsn)
    monkeypatch.setenv("T12_EVALUATOR_DSN", pg.evaluator_dsn)
    monkeypatch.setenv("T12_NEO4J_USER", "neo4j")
    monkeypatch.setenv("T12_NEO4J_PASSWORD", "reckoner-test-only")
    out = tmp_path / "evidence"
    out.mkdir()

    def guarded(dsn, directory=out):
        return [
            "--output-dir",
            directory,
            "--artifact-root",
            directory,
            "--pg-dsn-env",
            dsn,
            "--scaler",
            tmp_path / "scaler.json",
            "--store-bytes",
            "postgres=1",
        ]

    graph = ["--neo4j-uri", os.environ["RECKONER_TEST_NEO4J_URI"]]
    graph += ["--neo4j-user-env", "T12_NEO4J_USER", "--neo4j-password-env", "T12_NEO4J_PASSWORD"]
    for name in samples:
        graph += ["--sample-container", name]
    graph += ["--interval", "0.5"]

    # Inventory: the only privileged oracle read; Python-sorted hashing of every population.
    assert (
        _cli("inventory", *guarded("T12_OWNER_DSN"), "--bundle", bundle, "--query-date", query_date)
        == 0
    )
    protocol = json.loads((out / "protocol.json").read_text())
    members = [json.loads(line) for line in (out / "candidates.jsonl").read_text().splitlines()]
    union = sorted(tx["transaction_id"] for tx in members)
    assert protocol["candidate_union_count"] == len(union) > 0
    assert protocol["candidate_union_hash"] == content_id(union)
    for population in protocol["populations"]:
        assert population["candidate_ids"] == sorted(population["candidate_ids"])
        assert population["candidate_hash"] == content_id(population["candidate_ids"])
    assert protocol["candidate_policy"]["resolution_window_days"] == 90
    # Pre-flight: an existing output refuses the step before any store work.
    assert (
        _cli("inventory", *guarded("T12_OWNER_DSN"), "--bundle", bundle, "--query-date", query_date)
        == 2
    )
    assert "refusing to overwrite existing evidence" in capsys.readouterr().err

    # Prepare is one transaction: a guard failure after the first COPY batch stores nothing.
    failed = tmp_path / "failed-prepare"
    failed.mkdir()
    for name in ("protocol.json", "candidates.jsonl"):
        (failed / name).write_bytes((out / name).read_bytes())
    start = sum(p.stat().st_size for p in failed.iterdir())
    assert (
        _cli(
            "prepare",
            *guarded("T12_OWNER_DSN", failed),
            "--bundle",
            bundle,
            "--source-dir",
            source,
            "--batch-size",
            "1",
            "--guard-every",
            "1",
            "--derived-cap-bytes",
            start + 2,
        )
        == 2
    )
    assert "derived data above cap" in capsys.readouterr().err
    with psycopg.connect(pg.owner_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM reckoner.v1_vectors").fetchone()[0] == 0
    assert (
        _cli("prepare", *guarded("T12_OWNER_DSN"), "--bundle", bundle, "--source-dir", source) == 0
    )
    preparation = json.loads((out / "preparation.json").read_text())
    assert preparation["count"] == len(union)
    with psycopg.connect(pg.owner_dsn) as connection:
        stored = [
            r[0] for r in connection.execute("SELECT transaction_id FROM reckoner.v1_vectors")
        ]
    assert sorted(stored) == union

    assert _cli("verify", *guarded("T12_OWNER_DSN")) == 0
    verification = json.loads((out / "stored-verification.json").read_text())
    assert verification["stored_hash"] == protocol["candidate_union_hash"]
    assert all(row["complete"] for row in verification["coverage"])

    # A runtime session able to use schema oracle is refused before any timed work.
    refused = tmp_path / "refused"
    refused.mkdir()
    (refused / "protocol.json").write_bytes((out / "protocol.json").read_bytes())
    assert (
        _cli(
            "measure",
            *guarded("T12_EVALUATOR_DSN", refused),
            *graph,
            "--runtime-role",
            "reckoner_evaluator",
            "--first-pass-state",
            "refused",
        )
        == 2
    )
    assert "can use schema oracle" in capsys.readouterr().err
    assert not (refused / "retrieval-report.json").exists()

    assert (
        _cli(
            "measure",
            *guarded("T12_RUNNER_DSN"),
            *graph,
            "--first-pass-state",
            "disposable fixture, no restart",
        )
        == 0
    )
    measured = json.loads((out / "retrieval-report.json").read_text())
    assert measured["runtime_identity"]["current_user"] == "reckoner_runner"
    assert measured["runtime_identity"]["oracle_usage"] is False
    assert measured["runtime_identity"]["neo4j_user"] == "neo4j"
    assert measured["resource_samples_complete"] is True
    assert measured["provider_calls"] == 0 and measured["dataset_simulated"] is True
    assert measured["exact_neighbourhood_matches"] == measured["query_count"]
    assert measured["exact_candidate_matches"] == measured["query_count"]
    rows = [json.loads(x) for x in (out / "service-samples.jsonl").read_text().splitlines()]
    assert {row["Name"] for row in rows} == set(samples)

    assert (
        _cli(
            "replacement",
            *guarded("T12_RUNNER_DSN"),
            *graph,
            "--reason",
            "disposable fixture end-to-end check",
        )
        == 0
    )
    receipt = json.loads((out / "replacement-first-pass.json").read_text())
    body = {k: v for k, v in receipt.items() if k != "receipt_id"}
    assert receipt["receipt_id"] == content_id(body)
    assert receipt["resource_samples_complete"] is True
    assert receipt["runtime_identity"]["neo4j_user"] == "neo4j"

    assert (
        _cli(
            "audit",
            "--output-dir",
            out,
            "--artifact-root",
            out,
            "--container-prefix",
            audit_prefix,
            "--du-image",
            du_image,
            "--label",
            "fixture",
        )
        == 0
    )
    audit = json.loads((out / "resource-reconciliation-fixture.json").read_text())
    assert {c["name"].lstrip("/") for c in audit["containers"]} >= set(samples)

    oracle = [
        json.loads(line) for line in (bundle / "oracle_validation.jsonl").read_text().splitlines()
    ]
    labels = {row["transaction_id"]: row["label"] for row in oracle}
    (tmp_path / "labels.jsonl").write_text(
        "".join(
            json.dumps({"transaction_id": q, "label": labels[q]}) + "\n"
            for q in measured["query_ids"]
        )
    )
    (tmp_path / "annotations.json").write_text(
        json.dumps(
            {
                "schema_version": "retrieval-report-annotations-v1",
                "limitations": ["Fabricated simulated fixture; plumbing evidence only."],
                "relevance_interpretation": "Diagnostic only on fabricated data.",
                "first_pass_protocol": "Replacement pass on a disposable fixture; no restart.",
                "resource_accounting_stage": "After the replacement pass, fixture still running.",
            }
        )
    )
    report = tmp_path / "report" / "benchmark-report"
    assert (
        _cli(
            "report",
            "--protocol",
            out / "protocol.json",
            "--observation",
            out / "retrieval-report.json",
            "--oracle-labels",
            tmp_path / "labels.jsonl",
            "--annotations",
            tmp_path / "annotations.json",
            "--replacement",
            out / "replacement-first-pass.json",
            "--resources",
            out / "resource-reconciliation-fixture.json",
            "--samples",
            out / "service-samples.jsonl",
            "--output",
            report,
        )
        == 0
    )
    assembled = json.loads(report.with_name("benchmark-report.json").read_text())
    assert assembled["report_id"] == content_id(
        {k: v for k, v in assembled.items() if k != "report_id"}
    )
    assert {r["query_id"] for r in assembled["vector_results"]} == set(measured["query_ids"])
    _clear_graph(graph_driver)

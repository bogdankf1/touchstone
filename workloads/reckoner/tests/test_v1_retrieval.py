from copy import deepcopy

import pytest
from evidence_fixtures import coverage, records
from reckoner.contracts import content_id
from reckoner.v1.contracts import validate_v1
from reckoner.v1.data.history import historical_resolution
from reckoner.v1.evidence.assemble import assemble_evidence
from reckoner.v1.evidence.features import fit_scaler
from reckoner.v1.evidence.postgres import PostgresEvidence, import_evidence
from test_v1_features import observation
from v1_fixtures import config_fixture, evidence_fixture


def config():
    result = config_fixture()
    result["scaler"] = fit_scaler([observation(1), observation(9)])
    result["scaler_id"] = result["scaler"]["scaler_id"]
    return result


@pytest.mark.integration
def test_real_pgvector_cutoffs_ties_future_invariance_and_privileges(pg):
    query, rows, identities = records()
    cfg = config()
    import_evidence(pg.owner_dsn, [query], rows, identities, coverage(), cfg["scaler"])
    relational = PostgresEvidence(pg.runner_dsn)
    task = {"transaction": query, "query_time": query["occurred_at"]}
    before = relational.for_task(task, cfg)
    assert [r["transaction_id"] for r in before["comparable_cases"]] == ["a", "b", "c", "d", "e"]
    assert "unresolved" not in str(before["comparable_cases"])
    assert before["neighbourhood"]["total_transactions"] == 8
    assert before["neighbourhood"]["cross_tenant"] is True
    future = {**query, "transaction_id": "later", "occurred_at": "2019-01-01T00:00:00Z"}
    import_evidence(
        pg.owner_dsn,
        [],
        [
            {
                "transaction": future,
                "resolution": historical_resolution(
                    future, "legitimate", "simulated-seven-days-v1"
                ),
            }
        ],
        [],
        [],
        cfg["scaler"],
        include_vectors=False,
    )
    assert before == relational.for_task(task, cfg)
    missing_graph = assemble_evidence(task, cfg, relational, None)
    assert missing_graph["coverage"]["status"] == "partial"
    assert "graph unavailable" in missing_graph["coverage"]["missing"]
    with pytest.raises(ValueError, match="query time"):
        relational.for_task({**task, "query_time": "2019-01-01T00:00:00Z"}, cfg)


def test_old_evidence_fixtures_remain_valid_and_new_projection_times_checked():
    validate_v1("evidence", evidence_fixture())
    document = deepcopy(evidence_fixture())
    document["graph_projection"] = {"cutoff": "2020-01-01T00:00:00Z"}
    document["evidence_id"] = content_id({k: v for k, v in document.items() if k != "evidence_id"})
    with pytest.raises(ValueError):
        validate_v1("evidence", document)


@pytest.mark.integration
def test_unknown_history_and_missing_vectors_do_not_report_empty_success(pg):
    query, rows, identities = records()
    cfg = config()
    import_evidence(
        pg.owner_dsn, [query], rows, identities, [], cfg["scaler"], include_vectors=False
    )
    adapter = PostgresEvidence(pg.runner_dsn)
    task = {"transaction": query}
    result = adapter.for_task(task, cfg)
    assert result["coverage"]["status"] == "unavailable"
    assert result["features"]["prior_24h_count"] is None
    import_evidence(pg.owner_dsn, [], [], [], coverage(), cfg["scaler"], include_vectors=False)
    result = adapter.for_task(task, cfg)
    assert result["coverage"]["status"] == "partial"
    assert "eligible comparable vectors unavailable" in result["coverage"]["missing"]
    assert result["comparable_cases"] == []


def test_display_bounds_preserve_totals_and_reject_false_provenance():
    from reckoner.v1.evidence.assemble import document, neighbourhood

    query, rows, _ = records()
    records_many = [
        {
            "transaction": {**rows[0]["transaction"], "transaction_id": f"tx-{i}"},
            "merchant_identity": "shared",
        }
        for i in range(120)
    ]
    display = neighbourhood(records_many)
    assert len(display["nodes"]) == 100
    assert len(display["edges"]) <= 200
    assert display["total_nodes"] == 123
    assert display["total_transactions"] == 120
    assert display["truncated"] is True
    validated = document(
        query, "a" * 64, {"status": "available", "missing": []}, {}, display=display
    )
    validated["neighbourhood"]["truncated"] = False
    validated["evidence_id"] = content_id(
        {k: v for k, v in validated.items() if k != "evidence_id"}
    )
    with pytest.raises(ValueError, match="truncation"):
        validate_v1("evidence", validated)


@pytest.mark.integration
def test_candidate_vectors_require_complete_individual_history(pg):
    query, rows, identities = records()
    rows[0].pop("history", None)
    cfg = config()
    with pytest.raises(ValueError, match="candidate history"):
        import_evidence(pg.owner_dsn, [query], rows, identities, coverage(), cfg["scaler"])


def test_full_projection_provenance_rejects_later_cutoff_and_wrong_age():
    from reckoner.v1.evidence.assemble import document
    from reckoner.v1.evidence.neo4j import PARAMETERS

    query, _, _ = records()
    receipt = {
        "cutoff": "2018-06-02T00:00:00Z",
        "window_days": 30,
        "gds_version": "2026.09.0",
        "algorithm": "louvain-page-rank-v1",
        "parameters": PARAMETERS,
        "node_count": 4,
        "edge_count": 6,
        "covered_accounts": 1,
        "covered_cards": 1,
        "covered_merchants": 2,
        "build_seconds": 1.0,
        "page_rank_converged": True,
        "page_rank_iterations": 3,
        "community_count": 1,
        "cross_tenant": True,
    }
    receipt["projection_id"] = content_id(receipt)
    receipt["snapshot_age_seconds"] = "0"
    with pytest.raises(ValueError, match="cutoff|projection"):
        document(query, "a" * 64, {"status": "available", "missing": []}, {}, projection=receipt)
    receipt["cutoff"] = "2018-06-01T00:00:00Z"
    receipt["projection_id"] = content_id(
        {k: v for k, v in receipt.items() if k not in {"projection_id", "snapshot_age_seconds"}}
    )
    with pytest.raises(ValueError, match="age"):
        document(query, "a" * 64, {"status": "available", "missing": []}, {}, projection=receipt)


@pytest.mark.integration
def test_unsupported_comparables_preserve_history_and_report_exclusions(pg):
    from reckoner.v1.data.history import historical_resolution

    query, rows, identities = records()
    for amount in (0, -1000):
        tx = {
            **rows[0]["transaction"],
            "transaction_id": f"unsupported-{amount}",
            "amount_minor": amount,
        }
        rows.append(
            {
                "transaction": tx,
                "resolution": historical_resolution(tx, "legitimate", "simulated-seven-days-v1"),
            }
        )
    cfg = config()
    import_evidence(pg.owner_dsn, [query], rows, identities, coverage(), cfg["scaler"])
    result = PostgresEvidence(pg.runner_dsn).for_task({"transaction": query}, cfg)
    assert result["coverage"]["status"] == "available"
    assert result["features"]["excluded_unsupported_comparables"] == "2"
    assert result["neighbourhood"]["total_transactions"] == 10
    assert [r["transaction_id"] for r in result["comparable_cases"]] == ["a", "b", "c", "d", "e"]


@pytest.mark.integration
def test_supported_case_population_is_accessible_only_through_cutoff_adapter(pg):
    query, rows, identities = records()
    cfg = config()
    import_evidence(pg.owner_dsn, [query], rows, identities, coverage(), cfg["scaler"])
    assert {
        r["transaction_id"]
        for r in PostgresEvidence(pg.runner_dsn).resolved_cases({"transaction": query})
    } == {"a", "b", "c", "d", "e", "z"}

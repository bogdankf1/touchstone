"""Regressions for accepted generic measurements outside the saved baseline shape."""

import json
from dataclasses import replace
from decimal import Decimal

import duckdb
from fastapi.testclient import TestClient
from touchstone_platform.api import create_app
from touchstone_platform.contracts import canonical_sha256, validate_event
from touchstone_platform.query import SnapshotReader

from .test_metrics import declaration, event
from .test_semantics import build_marts

pytest_plugins = ("tests.test_query",)


def envelope(item, **fields):
    document = item.document
    for key, value in fields.items():
        if key in document["reproducibility"]:
            document["reproducibility"][key] = value
        else:
            document[key] = value
    return validate_event(document, item.received_at)


def declared(*, run_id="run-fixture-1", tenant_id="tenant-fixture-a", metric=False):
    item = declaration(("a",))
    document = item.document
    document.update(run_id=run_id, tenant_id=tenant_id)
    if metric:
        document["metric_expectations"] = [
            {
                "metric_id": "fixture-rate",
                "definition_version": "v1",
                "unit": "count",
                "expected_task_ids": ["a"],
            }
        ]
    return replace(
        item,
        identity=replace(item.identity, run_id=run_id, tenant_id=tenant_id),
        _document_json=json.dumps(document),
        content_sha256=canonical_sha256(document),
    )


def test_descendant_costs_roll_up_once_and_unexpected_membership_is_visible(tmp_path):
    scenarios = {
        "descendant": [
            event("execution", "child", parent_task_id="a"),
            event("execution", "grandchild", parent_task_id="child"),
            event("provider_usage", "grandchild", cost_amount="7"),
            event("provider_usage", "grandchild", cost_amount="7"),
        ],
        "unexpected": [
            event("execution", "unknown"),
            event("provider_usage", "unknown", cost_amount="7"),
        ],
        "ambiguous_parent": [
            event("execution", "child", parent_task_id="a"),
            event("execution", "child", event_id="changed-parent", parent_task_id="unknown"),
            event("provider_usage", "child", cost_amount="7"),
        ],
        "orphan_call": [event("provider_usage", "unknown", cost_amount="7")],
        "foreign_parent": [
            event("execution", "child", parent_task_id="foreign"),
            event("provider_usage", "child", cost_amount="7"),
        ],
        "cycle": [
            event("execution", "child", parent_task_id="grandchild"),
            event("execution", "grandchild", parent_task_id="child"),
            event("provider_usage", "child", cost_amount="7"),
        ],
    }
    events, declarations = [], []
    for run, extra in scenarios.items():
        declarations.append(declared(run_id=run))
        events.extend(
            envelope(item, run_id=run)
            for item in [event("execution", "a"), event("outcome", "a"), *extra]
        )
    # Matching IDs elsewhere must not supply ancestry in the selected tenant/run.
    events.append(
        envelope(
            event("execution", "foreign", parent_task_id="a"),
            run_id="foreign_parent",
            tenant_id="other-tenant",
        )
    )
    events.append(envelope(event("execution", "foreign", parent_task_id="a"), run_id="other-run"))
    warehouse = build_marts(tmp_path, events, declarations)
    with duckdb.connect(str(warehouse), read_only=True) as db:
        assert db.execute(
            "select expected_tasks, received_tasks, model_cost, metrics_complete "
            "from mart_runs where run_id='descendant'"
        ).fetchone() == (1, 1, Decimal("7"), True)
        assert db.execute(
            "select task_id, model_cost, call_count from mart_nodes where run_id='descendant'"
        ).fetchall() == [("a", Decimal("7"), 1)]
        for run in ("unexpected", "orphan_call", "foreign_parent", "cycle", "ambiguous_parent"):
            cost, complete, unexpected = db.execute(
                "select model_cost, metrics_complete, unexpected_tasks from mart_runs "
                "where run_id=? and tenant_id='tenant-fixture-a'",
                [run],
            ).fetchone()
            assert cost is None
            assert complete is False
            assert unexpected > 0
            assert (
                db.execute(
                    "select sum(model_cost) from mart_nodes where run_id=? "
                    "and tenant_id='tenant-fixture-a'",
                    [run],
                ).fetchone()[0]
                == 7
            )


def test_correctness_depends_on_outcome_evidence_not_price_evidence(tmp_path):
    cases = {
        "unpriced": [
            event("outcome", "a"),
            event(
                "provider_usage",
                "a",
                cost_status="unavailable",
                cost_amount=None,
                currency=None,
                price_table_version=None,
            ),
        ],
        "wrong_price_version": [
            event("outcome", "a"),
            envelope(event("provider_usage", "a"), config_version="other"),
        ],
        "unknown_outcome": [],
        "wrong_outcome_version": [envelope(event("outcome", "a"), config_version="other")],
    }
    events = [
        envelope(item, run_id=run)
        for run, items in cases.items()
        for item in [event("execution", "a"), *items]
    ]
    warehouse = build_marts(tmp_path, events, [declared(run_id=run) for run in cases])
    with duckdb.connect(str(warehouse), read_only=True) as db:
        reader = SnapshotReader(db, {}, {})
        for run in ("unpriced", "wrong_price_version"):
            summary = reader.summary("reckoner", run, tenant_id="tenant-fixture-a")
            assert summary["correct_tasks"] == 1
            assert summary["correctness"] == 1
            assert summary["model_cost"] is None
            assert summary["cpst"] is None
            assert summary["metrics_complete"] is False
        for run in ("unknown_outcome", "wrong_outcome_version"):
            summary = reader.summary("reckoner", run, tenant_id="tenant-fixture-a")
            assert summary["correct_tasks"] is None
            assert summary["correctness"] is None


def test_contribution_eligibility_checks_own_versions_and_mode(tmp_path):
    changes = {
        key: {key: "other"}
        for key in (
            "workflow_version",
            "experiment_version",
            "cohort_version",
            "config_version",
            "code_revision",
            "dataset_version",
        )
    }
    changes["mode"] = {"simulated": True}
    changes["independent"] = {}
    events = [
        envelope(event("metric_contribution", "a"), run_id=run, **fields)
        for run, fields in changes.items()
    ]
    warehouse = build_marts(
        tmp_path, events, [declared(run_id=run, metric=True) for run in changes]
    )
    with duckdb.connect(str(warehouse), read_only=True) as db:
        for run in changes:
            assert db.execute(
                "select numerator_sum, denominator_sum, incomplete_contributions, rate "
                "from mart_contribution_rates where run_id=?",
                [run],
            ).fetchone() == (
                1,
                2,
                0 if run == "independent" else 1,
                0.5 if run == "independent" else None,
            )


def test_slash_run_id_reads_published_summary_and_tasks(published):
    with duckdb.connect(str(published.warehouse_dir / "generation-one.duckdb")) as db:
        for table in (
            "mart_runs",
            "raw_declarations",
            "int_calls",
            "int_tasks",
            "mart_nodes",
            "stg_events",
            "raw_rejections",
            "mart_contribution_rates",
        ):
            db.execute(f"update {table} set run_id='a/b' where run_id='run'")
    client = TestClient(create_app(published))
    params = {"workflow_id": "workflow", "tenant_id": "tenant-a"}
    summary = client.get("/v1/runs/a%2Fb/summary", params=params)
    assert summary.status_code == 200, summary.text
    assert summary.json()["data"]["run_id"] == "a/b"
    tasks = client.get("/v1/runs/a%2Fb/tasks", params=params)
    assert tasks.status_code == 200, tasks.text
    assert tasks.json()["data"]["total"] == 2
    assert {row["run_id"] for row in tasks.json()["data"]["items"]} == {"a/b"}


def test_aggregate_money_preserves_all_supported_digits(published):
    with duckdb.connect(str(published.warehouse_dir / "generation-one.duckdb")) as db:
        db.execute(
            "update mart_runs set model_cost=9999999999999999.123456789012, "
            "metrics_complete=true where run_id='run'"
        )
    from touchstone_platform.query import open_snapshot

    with open_snapshot(published) as reader:
        summary = reader.summary("workflow", "run", aggregate=True)
    assert summary["model_cost"] == "19999999999999998.246913578024"
    assert summary["cpst"] == "4999999999999999.561728394506"

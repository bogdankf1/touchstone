"""Independent generic accounting fixtures; simulated, never provider evidence."""

import json
from dataclasses import replace
from decimal import Decimal

import duckdb
import pytest
from touchstone_platform.contracts import canonical_sha256, validate_event

from .test_metrics import RECEIVED, declaration, event
from .test_semantics import build_marts


def scoped_call(
    call_id, *, scope="online", provider="one", model="model-one", price="price-one", amount="0.1"
):
    return event(
        "provider_usage",
        "a",
        event_id=call_id,
        call_id=call_id,
        provider=provider,
        model=model,
        price_table_version=price,
        cost_scope=scope,
        cost_amount=amount,
        cost_status="actual" if amount is not None else "unavailable",
        currency="USD" if amount is not None else None,
    )


def work(kind, payload):
    doc = event("execution", "a").document
    doc.update(event_kind=kind, event_id=kind + str(payload.get("cost_scope", "")), payload=payload)
    return validate_event(doc, RECEIVED)


def lifecycle():
    d = declaration(("a",))
    doc = d.document | {"lifecycle_version": "root-work-v1"}
    return replace(d, _document_json=json.dumps(doc), content_sha256=canonical_sha256(doc))


def closures(online=("score", "note"), offline=("judge",)):
    return [
        work("work_declaration", {"child_task_ids": [], "evaluation_suites": []}),
        *[
            work(
                "work_closure",
                {
                    "cost_scope": scope,
                    "call_ids": list(ids),
                    "work_status": "completed",
                    "billing_status": "complete",
                },
            )
            for scope, ids in (("online", online), ("offline", offline))
        ],
    ]


@pytest.mark.parametrize(
    "offline_amount,spend,offline_complete", [("0.7", Decimal("1.0"), True), (None, None, False)]
)
def test_two_providers_online_subtotal_independent_of_offline_billing(
    tmp_path, offline_amount, spend, offline_complete
):
    calls = [
        scoped_call("score"),
        scoped_call("note", provider="two", model="model-two", price="price-two", amount="0.2"),
        scoped_call(
            "judge",
            scope="offline",
            provider="two",
            model="model-two",
            price="price-two",
            amount=offline_amount,
        ),
    ]
    warehouse = build_marts(
        tmp_path,
        [event("execution", "a"), event("outcome", "a"), *calls, calls[0], *closures()],
        [lifecycle()],
    )
    with duckdb.connect(str(warehouse)) as c:
        assert c.execute(
            "select model_cost, provider_spend, online_cost_complete, offline_cost_complete, "
            "latency_p99_ms from mart_runs"
        ).fetchone() == (Decimal("0.3"), spend, True, offline_complete, 1000.0)
        assert c.execute("select count(*) from int_calls").fetchone() == (3,)
        assert c.execute("select model_cost,provider_spend from mart_nodes").fetchone() == (
            Decimal("0.3"),
            spend,
        )


@pytest.mark.parametrize("missing", ["declaration", "online", "call", "conflict"])
def test_missing_or_conflicting_work_cannot_finalize_online_cost(tmp_path, missing):
    work_events = closures(online=("score",), offline=())
    if missing == "declaration":
        work_events = work_events[1:]
    elif missing == "online":
        work_events = [work_events[0], work_events[2]]
    calls = [] if missing == "call" else [scoped_call("score")]
    if missing == "conflict":
        calls.append(scoped_call("score", scope="offline"))
    warehouse = build_marts(
        tmp_path,
        [event("execution", "a"), event("outcome", "a"), *calls, *work_events],
        [lifecycle()],
    )
    with duckdb.connect(str(warehouse)) as c:
        assert c.execute("select model_cost, online_cost_complete from mart_runs").fetchone() == (
            None,
            False,
        )


@pytest.mark.parametrize("workflow", ["reckoner", "document-routing"])
def test_late_quality_population_is_declared_before_results_and_online_cost_stays_known(
    tmp_path, workflow
):
    d = lifecycle()
    doc = d.document
    doc["workflow_id"] = workflow
    doc["evaluation_suites"] = [
        {
            "suite_id": "late-quality",
            "suite_version": "v1",
            "expected_case_ids": [],
            "required_checks": ["quality"],
        }
    ]
    d = replace(
        d,
        identity=replace(d.identity, workflow_id=workflow),
        _document_json=json.dumps(doc),
        content_sha256=canonical_sha256(doc),
    )
    events = [
        event("execution", "a"),
        event("outcome", "a"),
        scoped_call("score"),
        *closures(online=("score",), offline=()),
    ]
    for index, e in enumerate(events):
        doc = e.document
        doc["workflow_id"] = workflow
        if doc["event_kind"] == "work_declaration":
            doc["payload"]["evaluation_suites"] = [
                {
                    "suite_id": "late-quality",
                    "suite_version": "v1",
                    "expected_case_ids": ["case-late"],
                    "required_checks": ["quality"],
                }
            ]
        events[index] = validate_event(doc, RECEIVED)
    warehouse = build_marts(tmp_path, events, [d])
    with duckdb.connect(str(warehouse)) as c:
        assert c.execute(
            "select "
            "model_cost,online_cost_complete,expected_cases,missing_checks,metrics_complete "
            "from mart_runs"
        ).fetchone() == (Decimal(".1"), True, 1, 1, False)


def test_unregistered_late_suite_cannot_retrofit_population(tmp_path):
    events = [
        event("execution", "a"),
        event("outcome", "a"),
        scoped_call("score"),
        *closures(online=("score",), offline=()),
    ]
    doc = events[3].document
    doc["payload"]["evaluation_suites"] = [
        {
            "suite_id": "unregistered",
            "suite_version": "v1",
            "expected_case_ids": [],
            "required_checks": ["invented"],
        }
    ]
    events[3] = validate_event(doc, RECEIVED)
    warehouse = build_marts(tmp_path, events, [lifecycle()])
    with duckdb.connect(str(warehouse)) as c:
        assert c.execute("select online_cost_complete from mart_runs").fetchone() == (False,)


def test_new_call_after_closure_cannot_disappear(tmp_path):
    events = [
        event("execution", "a"),
        event("outcome", "a"),
        scoped_call("score"),
        scoped_call("unclosed-call"),
        *closures(online=("score",), offline=()),
    ]
    warehouse = build_marts(tmp_path, events, [lifecycle()])
    with duckdb.connect(str(warehouse)) as c:
        assert c.execute("select model_cost,online_cost_complete from mart_runs").fetchone() == (
            None,
            False,
        )


def test_summary_exposes_scope_totals_and_independent_completeness(tmp_path):
    from touchstone_platform.query import SnapshotReader

    events = [
        event("execution", "a"),
        event("outcome", "a"),
        scoped_call("score"),
        scoped_call("judge", scope="offline", amount="0.7"),
        *closures(online=("score",), offline=("judge",)),
    ]
    warehouse = build_marts(tmp_path, events, [lifecycle()])
    with duckdb.connect(str(warehouse)) as c:
        reader = SnapshotReader(c, {"generation": "fixed-generation"}, {})
        row = reader.summary("reckoner", "run-fixture-1", tenant_id="tenant-fixture-a")
        assert row.get("provider_spend") == "0.800000000000"
        assert row["offline_model_cost"] == "0.700000000000"
        assert row["online_cost_complete"] is True
        assert row["cpst"] == "0.100000000000"


def test_declared_provider_price_binding_rejects_wrong_single_snapshot(tmp_path):
    d = lifecycle()
    doc = d.document
    doc["provider_prices"] = [
        {"provider": "one", "model": "model-one", "price_table_version": "pinned-price"}
    ]
    from touchstone_platform.contracts import validate_declaration

    validate_declaration(doc)
    d = replace(d, _document_json=json.dumps(doc), content_sha256=canonical_sha256(doc))
    warehouse = build_marts(
        tmp_path,
        [
            event("execution", "a"),
            event("outcome", "a"),
            scoped_call("score"),
            *closures(online=("score",), offline=()),
        ],
        [d],
    )
    with duckdb.connect(str(warehouse)) as c:
        assert c.execute("select model_cost,online_cost_complete from mart_runs").fetchone() == (
            None,
            False,
        )


def test_real_otlp_refresh_publishes_exact_fixture_and_second_workflow(tmp_path):
    import os
    import time
    from datetime import UTC, datetime
    from pathlib import Path

    import clickhouse_connect
    from fastapi.testclient import TestClient
    from reckoner.v1.telemetry.events import reconcile_report
    from reckoner.v1.telemetry.exporter import OTLPExporter, encode
    from touchstone_platform.api import create_app
    from touchstone_platform.extract import iter_measurements
    from touchstone_platform.refresh import refresh
    from touchstone_platform.settings import Settings

    endpoint = os.environ.get("RECKONER_TEST_OTLP_ENDPOINT")
    if not endpoint:
        pytest.skip("requires dedicated Task10 Collector fixture")
    expected = json.loads(
        (
            Path(__file__).parents[2] / "workloads/reckoner/expectations/reckoner-v1-local.json"
        ).read_text()
    )
    exporter = OTLPExporter(endpoint)
    documents = []
    for workflow in ("reckoner", "document-routing"):
        run_id = "task10-local-" + workflow
        d = lifecycle().document
        d.update(workflow_id=workflow, run_id=run_id, event_id="declare-" + run_id)
        d["metric_expectations"] = [
            {
                "metric_id": "fixture-rate",
                "definition_version": "v1",
                "unit": "count",
                "expected_task_ids": ["a"],
            }
        ]
        documents.append(d)
        events = [
            event("execution", "a"),
            event("outcome", "a", review_cost="4"),
            scoped_call("score"),
            scoped_call(
                "note",
                provider="two",
                model="model-two",
                price="price-two",
                amount=".2".replace(".", "0.", 1),
            ),
            scoped_call(
                "judge",
                scope="offline",
                provider="two",
                model="model-two",
                price="price-two",
                amount="0.7",
            ),
            event("metric_contribution", "a", numerator=2, denominator=3),
            *closures(),
        ]
        for e in events:
            doc = e.document
            doc.update(
                workflow_id=workflow,
                run_id=run_id,
                trace_id="a" * 32,
                span_id=canonical_sha256(doc)[:16],
            )
            documents.append(doc)
    for doc in [*documents, documents[2], documents[-1]]:
        assert exporter.export(encode(doc))["accepted"]
    settings = Settings(
        warehouse_dir=tmp_path / "published",
        clickhouse_host="127.0.0.1",
        clickhouse_port=int(os.environ["RECKONER_TEST_CLICKHOUSE_PORT"]),
        clickhouse_username="touchstone",
        clickhouse_password="fabricated-task10-local",
        dbt_threads=1,
        duckdb_memory_limit="1GB",
    )
    client = clickhouse_connect.get_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_username,
        password=settings.clickhouse_password,
    )
    try:
        for _ in range(40):
            received = [
                e
                for e in iter_measurements(client, through=datetime.now(UTC))
                if hasattr(e, "run_id") and e.run_id.startswith("task10-local-")
            ]
            if len(received) >= 20:
                break
            time.sleep(0.25)
        assert len(received) >= 20
    finally:
        client.close()
    result = refresh(settings)
    api = TestClient(create_app(settings))
    for workflow in ("reckoner", "document-routing"):
        response = api.get(
            "/v1/runs/task10-local-" + workflow + "/summary",
            params={"workflow_id": workflow, "tenant_id": "tenant-fixture-a"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        row = body["data"]
        assert body["metadata"]["generation"] == result.generation
        keys = (
            "tenant_id",
            "expected_tasks",
            "received_tasks",
            "completed_tasks",
            "correct_tasks",
            "model_cost",
            "offline_model_cost",
            "provider_spend",
            "review_cost",
            "error_cost",
            "cpst",
            "latency_p99_ms",
        )
        literal = {k: expected[k] for k in keys}
        assert reconcile_report(literal, {k: row[k] for k in keys}, literal)["matched"]
        assert (
            row["metrics_complete"] and row["online_cost_complete"] and row["offline_cost_complete"]
        )
        metric = row["contribution_rates"][0]
        assert (metric["numerator"], metric["denominator"]) == (2, 3)
        tasks = api.get(
            "/v1/runs/task10-local-" + workflow + "/tasks",
            params={"workflow_id": workflow, "tenant_id": "tenant-fixture-a"},
        ).json()
        assert tasks["metadata"]["generation"] == result.generation
        task = tasks["data"]["items"][0]
        assert task["task_id"] == expected["task_id"]
        assert sum(n["call_count"] for n in task["nodes"]) == expected["call_count"]
        assert task["nodes"][0]["node_name"] == expected["root_node"]


def test_offline_score_only_spend_needs_no_routing_outcome(tmp_path):
    warehouse = build_marts(
        tmp_path,
        [
            event("execution", "a"),
            scoped_call("score", scope="offline", amount="0.000084"),
            *closures(online=(), offline=("score",)),
        ],
        [lifecycle()],
    )
    with duckdb.connect(str(warehouse)) as c:
        assert c.execute(
            "select model_cost,offline_model_cost,provider_spend,online_cost_complete,"
            "offline_cost_complete from mart_runs"
        ).fetchone() == (Decimal("0"), Decimal("0.000084"), Decimal("0.000084"), True, True)
        from touchstone_platform.query import SnapshotReader

        row = SnapshotReader(c, {"generation": "fixed-generation"}, {}).summary(
            "reckoner", "run-fixture-1", tenant_id="tenant-fixture-a"
        )
        assert row["cpst"] is None


@pytest.mark.parametrize("root_state", ["missing", "started", "conflicting", "failed", "completed"])
def test_scope_closure_requires_unambiguous_terminal_root(tmp_path, root_state):
    roots = []
    if root_state != "missing":
        roots = [
            event(
                "execution",
                "a",
                status="started"
                if root_state == "started"
                else ("failed" if root_state == "failed" else "completed"),
            )
        ]
    if root_state == "conflicting":
        roots.append(event("execution", "a", status="failed", event_id="other-terminal"))
    warehouse = build_marts(tmp_path, [*roots, *closures(online=(), offline=())], [lifecycle()])
    with duckdb.connect(str(warehouse)) as c:
        expected = root_state in {"failed", "completed"}
        assert c.execute(
            "select online_cost_complete,offline_cost_complete from mart_runs"
        ).fetchone() == (expected, expected)
    if root_state == "missing":
        late = tmp_path / "late"
        late.mkdir()
        warehouse = build_marts(
            late, [event("execution", "a"), *closures(online=(), offline=())], [lifecycle()]
        )
        with duckdb.connect(str(warehouse)) as c:
            assert c.execute(
                "select online_cost_complete,offline_cost_complete from mart_runs"
            ).fetchone() == (True, True)

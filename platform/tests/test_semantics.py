"""Exercise governed dbt marts with hand-computed simulated fixtures."""

import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
from test_metrics import declaration, event
from touchstone_platform.contracts import canonical_sha256, validate_event
from touchstone_platform.staging import build_snapshot

PROJECT = Path(__file__).resolve().parents[1] / "dbt"


def build_marts(tmp_path, events, declarations):
    warehouse = tmp_path / "warehouse.duckdb"
    build_snapshot(events, declarations, warehouse, through=datetime(2026, 9, 26, 12, tzinfo=UTC))
    profile = tmp_path / "profiles.yml"
    profile.write_text(
        f"touchstone:\n  target: local\n  outputs:\n    local:\n"
        f"      type: duckdb\n      path: {warehouse}\n"
        "      schema: main\n      threads: 2\n"
    )
    command = [
        "dbt",
        "build",
        "--project-dir",
        str(PROJECT),
        "--profiles-dir",
        str(tmp_path),
        "--target-path",
        str(tmp_path / "target"),
    ]
    result = subprocess.run(
        command, env=os.environ.copy(), capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout[-5000:] + result.stderr[-2000:]
    return warehouse


def test_cost_components_cpst_population_and_duplicate_calls(tmp_path):
    a_call = event("provider_usage", "a", cost_amount="0.1")
    b_call = event("provider_usage", "b", cost_amount="0.2")
    events = [
        event("execution", "a"),
        event("execution", "b"),
        a_call,
        a_call,
        b_call,
        event("outcome", "a", correct=True, review_cost="4", error_cost="0"),
        event("outcome", "b", correct=False, review_cost="0", error_cost="6"),
    ]
    warehouse = build_marts(tmp_path, events, [declaration()])
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        row = connection.execute(
            "select expected_tasks, received_tasks, completed_tasks, correct_tasks, "
            "model_cost, review_cost, error_cost, metrics_complete "
            "from mart_runs"
        ).fetchone()
        assert row[:4] == (2, 2, 2, 1)
        assert row[4:7] == (Decimal("0.3"), Decimal("4"), Decimal("6"))
        assert row[7] is True
        assert (row[4] + row[5] + row[6]) / row[3] == Decimal("10.3")
        assert connection.execute("select count(*) from int_calls").fetchone()[0] == 2


def test_missing_expected_task_is_incomplete_and_zero_correct_is_undefined(tmp_path):
    events = [event("execution", "a"), event("outcome", "a", correct=False)]
    warehouse = build_marts(tmp_path, events, [declaration()])
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        row = connection.execute(
            "select expected_tasks, missing_tasks, correct_tasks, metrics_complete from mart_runs"
        ).fetchone()
        assert row == (2, 1, 0, False)


def test_retries_are_one_task_with_all_distinct_call_costs_and_root_latency(tmp_path):
    first = event("execution", "a", event_id="attempt-1", status="failed")
    second = event(
        "execution",
        "a",
        event_id="attempt-2",
        attempt_number=2,
        started_at="2026-09-26T10:00:02Z",
        ended_at="2026-09-26T10:00:05Z",
    )
    call_1 = event("provider_usage", "a", event_id="call-1", call_id="first", cost_amount="0.1")
    call_2 = event("provider_usage", "a", event_id="call-2", call_id="second", cost_amount="0.2")
    child = event("execution", "child", event_id="child-execution", parent_task_id="a")
    warehouse = build_marts(
        tmp_path,
        [first, second, call_1, call_2, child, event("outcome", "a")],
        [declaration(("a",))],
    )
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        row = connection.execute(
            "select expected_tasks, received_tasks, completed_tasks, model_cost, "
            "latency_p99_ms from mart_runs"
        ).fetchone()
        assert row == (1, 1, 1, Decimal("0.3"), 5000.0)


def test_conflicting_outcomes_currency_and_config_do_not_publish_complete_sum(tmp_path):
    conflicting = event("outcome", "a", event_id="outcome-second", correct=False)
    eur = event("provider_usage", "b", currency="EUR")
    changed = event("execution", "b", event_id="changed-config")
    changed_doc = changed.document
    changed_doc["reproducibility"]["config_version"] = "other"
    from touchstone_platform.contracts import validate_event

    changed = validate_event(changed_doc, changed.received_at)
    warehouse = build_marts(
        tmp_path,
        [
            event("execution", "a"),
            changed,
            event("outcome", "a"),
            conflicting,
            event("outcome", "b"),
            event("provider_usage", "a"),
            eur,
        ],
        [declaration()],
    )
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        assert connection.execute("select metrics_complete from mart_runs").fetchone()[0] is False


def test_mixed_currency_and_conflicted_call_have_null_model_total(tmp_path):
    first = event("provider_usage", "a", cost_amount="0.1")
    conflict = event("provider_usage", "a", cost_amount="0.2")
    eur = event("provider_usage", "b", currency="EUR")
    warehouse = build_marts(
        tmp_path,
        [
            event("execution", "a"),
            event("execution", "b"),
            event("outcome", "a"),
            event("outcome", "b"),
            first,
            conflict,
            eur,
        ],
        [declaration()],
    )
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        model_cost, complete = connection.execute(
            "select model_cost, metrics_complete from mart_runs"
        ).fetchone()
        assert model_cost is None
        assert complete is False


def test_price_versions_across_tasks_do_not_form_one_model_total(tmp_path):
    warehouse = build_marts(
        tmp_path,
        [
            event("execution", "a"),
            event("execution", "b"),
            event("outcome", "a"),
            event("outcome", "b"),
            event("provider_usage", "a", price_table_version="price-v1"),
            event("provider_usage", "b", price_table_version="price-v2"),
        ],
        [declaration()],
    )
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        assert connection.execute(
            "select model_cost, metrics_complete from mart_runs"
        ).fetchone() == (None, False)


def test_declared_case_membership_requires_every_check(tmp_path):
    declared = declaration(("a", "b"))
    document = declared.document
    document["evaluation_suites"] = [
        {
            "suite_id": "fixture-deterministic",
            "suite_version": "v1",
            "expected_case_ids": ["a", "b"],
            "required_checks": ["correctness", "validity"],
        }
    ]
    declared = replace(
        declared, _document_json=json.dumps(document), content_sha256=canonical_sha256(document)
    )
    events = [
        event("execution", "a"),
        event("execution", "b"),
        event("outcome", "a"),
        event("outcome", "b"),
        event("evaluation", "a"),
        event("evaluation", "a", event_id="validity-a", metric_id="validity"),
    ]
    warehouse = build_marts(tmp_path, events, [declared])
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        assert connection.execute(
            "select expected_cases, passed_cases, missing_checks, metrics_complete from mart_runs"
        ).fetchone() == (2, 1, 2, False)


def test_undeclared_run_has_unknown_completeness(tmp_path):
    warehouse = build_marts(tmp_path, [event("execution", "a")], [])
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        assert connection.execute(
            "select completeness_known, expected_tasks, metrics_complete from mart_runs"
        ).fetchone() == (False, None, False)


def test_pending_outcome_is_incomplete_not_zero_error_cost(tmp_path):
    warehouse = build_marts(
        tmp_path,
        [
            event("execution", "a"),
            event(
                "outcome",
                "a",
                status="pending",
                correct=None,
                review_cost=None,
                error_cost=None,
                currency=None,
            ),
        ],
        [declaration(("a",))],
    )
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        assert connection.execute(
            "select review_cost, error_cost, metrics_complete from mart_runs"
        ).fetchone() == (None, None, False)


def test_generic_contribution_rate_sums_by_definition_and_nulls_zero(tmp_path):
    first = event("metric_contribution", "a", numerator=1, denominator=2)
    repeat = event("metric_contribution", "a", event_id="repeat-a", numerator=1, denominator=2)
    second = event("metric_contribution", "b", numerator=2, denominator=2)
    other_version = event(
        "metric_contribution",
        "a",
        event_id="v2-a",
        definition_version="v2",
        numerator=5,
        denominator=0,
    )
    warehouse = build_marts(tmp_path, [first, repeat, second, other_version], [declaration()])
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        rows = connection.execute(
            "select definition_version, numerator_sum, denominator_sum, rate "
            "from mart_contribution_rates order by definition_version"
        ).fetchall()
        assert rows == [("v1", 3.0, 4.0, None), ("v2", 5.0, 0.0, None)]


def test_measured_envelopes_on_simulated_dataset_match_measured_declaration(tmp_path):
    measured = []
    for item in [event("execution", "a"), event("outcome", "a"), event("provider_usage", "a")]:
        document = item.document
        document["simulated"] = False
        measured.append(validate_event(document, item.received_at))
    warehouse = build_marts(tmp_path, measured, [declaration(("a",))])
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        assert connection.execute(
            "select measurement_mode, dataset_simulated, metrics_complete from mart_runs"
        ).fetchone() == ("measured", True, True)


def test_rejected_unpriced_provider_cost_is_unknown_not_zero(tmp_path):
    too_precise = event("provider_usage", "a", cost_amount="0.0000000000001")
    warehouse = build_marts(
        tmp_path,
        [event("execution", "a"), event("outcome", "a"), too_precise],
        [declaration(("a",))],
    )
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        assert connection.execute(
            "select model_cost, metrics_complete from mart_runs"
        ).fetchone() == (None, False)
        assert connection.execute(
            "select tenant_id, workflow_id, run_id, task_id from raw_rejections"
        ).fetchone() == ("tenant-fixture-a", "reckoner", "run-fixture-1", "a")


def test_pending_result_can_resolve_to_unambiguous_observed_result(tmp_path):
    warehouse = build_marts(
        tmp_path,
        [
            event("execution", "a"),
            event(
                "outcome",
                "a",
                event_id="pending-a",
                status="pending",
                correct=None,
                review_cost=None,
                error_cost=None,
                currency=None,
            ),
            event("outcome", "a", event_id="observed-a"),
        ],
        [declaration(("a",))],
    )
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        assert connection.execute(
            "select correct_tasks, metrics_complete from mart_runs"
        ).fetchone() == (1, True)


def test_started_root_and_unfinished_latest_retry_are_incomplete(tmp_path):
    started = event("execution", "a", status="started", ended_at=None, duration_ms=None)
    warehouse = build_marts(tmp_path, [started, event("outcome", "a")], [declaration(("a",))])
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        assert connection.execute("select metrics_complete from mart_runs").fetchone()[0] is False
    first = event("execution", "a", event_id="first-terminal")
    later = event(
        "execution",
        "a",
        event_id="later-started",
        attempt_number=2,
        status="started",
        ended_at=None,
        duration_ms=None,
    )
    second_warehouse = build_marts(
        tmp_path / "later", [first, later, event("outcome", "a")], [declaration(("a",))]
    )
    with duckdb.connect(str(second_warehouse), read_only=True) as connection:
        assert connection.execute("select metrics_complete from mart_runs").fetchone()[0] is False


def test_fully_observed_failed_task_is_receipt_complete(tmp_path):
    warehouse = build_marts(
        tmp_path,
        [event("execution", "a", status="failed"), event("outcome", "a", correct=False)],
        [declaration(("a",))],
    )
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        assert connection.execute(
            "select failed_tasks, completed_tasks, metrics_complete from mart_runs"
        ).fetchone() == (1, 0, True)


def test_declared_contribution_population_gates_rate_but_keeps_components(tmp_path):
    declared = declaration(("a", "b"))
    document = declared.document
    document["metric_expectations"] = [
        {
            "metric_id": "fixture-rate",
            "definition_version": "v1",
            "unit": "count",
            "expected_task_ids": ["a", "b"],
        }
    ]
    declared = replace(
        declared, _document_json=json.dumps(document), content_sha256=canonical_sha256(document)
    )
    first = event("metric_contribution", "a", numerator=1, denominator=2)
    partial = build_marts(tmp_path / "partial", [first], [declared])
    with duckdb.connect(str(partial), read_only=True) as connection:
        assert connection.execute(
            "select expected_tasks, task_contributions, numerator_sum, "
            "denominator_sum, rate from mart_contribution_rates"
        ).fetchone() == (2, 1, 1.0, 2.0, None)
    second = event("metric_contribution", "b", numerator=2, denominator=2)
    full = build_marts(tmp_path / "full", [first, second], [declared])
    with duckdb.connect(str(full), read_only=True) as connection:
        assert connection.execute(
            "select expected_tasks, task_contributions, numerator_sum, "
            "denominator_sum, rate from mart_contribution_rates"
        ).fetchone() == (2, 2, 3.0, 4.0, 0.75)


def test_nearest_rank_p99_uses_only_selected_tenant(tmp_path):
    events = []
    tasks = [f"task-{i}" for i in range(1, 101)]
    for i, task in enumerate(tasks, start=1):
        ended = datetime(2026, 9, 26, 10, tzinfo=UTC) + timedelta(milliseconds=i)
        events.append(event("execution", task, ended_at=ended.isoformat(), duration_ms=i))
        events.append(event("outcome", task))
    other_event = event("execution", "other", event_id="other-execution", duration_ms=10000)
    other_document = other_event.document
    other_document["tenant_id"] = "tenant-fixture-b"
    other_document["run_id"] = "other-run"
    events.append(validate_event(other_document, other_event.received_at))
    other_declared = declaration(("other",))
    other_declaration_doc = other_declared.document
    other_declaration_doc["tenant_id"] = "tenant-fixture-b"
    other_declaration_doc["run_id"] = "other-run"
    other_declared = replace(
        other_declared,
        identity=replace(other_declared.identity, tenant_id="tenant-fixture-b", run_id="other-run"),
        _document_json=json.dumps(other_declaration_doc),
        content_sha256=canonical_sha256(other_declaration_doc),
    )
    warehouse = build_marts(tmp_path, events, [declaration(tuple(tasks)), other_declared])
    with duckdb.connect(str(warehouse), read_only=True) as connection:
        assert connection.execute(
            "select latency_population, latency_p99_ms from mart_runs "
            "where tenant_id = 'tenant-fixture-a'"
        ).fetchone() == (100, 99.0)

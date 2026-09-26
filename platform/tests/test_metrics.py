"""Contract fixtures for exact, repeatable warehouse staging."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from jsonschema import ValidationError
from touchstone_platform.contracts import validate_event
from touchstone_platform.extract import RejectedEvent, ValidatedDeclaration
from touchstone_platform.staging import build_snapshot, exact_cpst

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"
RECEIVED = "2026-09-26T12:00:00+00:00"


def event(kind, task="a", event_id=None, **payload):
    document = json.loads((EXAMPLES / "measurement-v1.json").read_text())
    document.update(event_id=event_id or f"{kind}-{task}", task_id=task, event_kind=kind)
    values = {
        "execution": {
            "started_at": "2026-09-26T10:00:00Z",
            "ended_at": "2026-09-26T10:00:01Z",
            "duration_ms": 1000,
            "status": "completed",
            "attempt_number": 1,
            "parent_task_id": None,
            "parent_span_id": None,
            "provider": None,
            "model": None,
        },
        "provider_usage": {
            "provider": "fixture",
            "model": "fixture",
            "call_id": f"call-{task}",
            "input_tokens": 1,
            "output_tokens": 1,
            "cached_tokens": 0,
            "cost_status": "actual",
            "cost_amount": "0.1",
            "currency": "USD",
            "price_table_version": "price-v1",
        },
        "outcome": {
            "status": "observed",
            "correct": True,
            "review_cost": "0",
            "error_cost": "0",
            "currency": "USD",
            "outcome_version": "v1",
            "evaluator_version": "v1",
        },
        "evaluation": {
            "suite_id": "fixture-deterministic",
            "case_id": task,
            "metric_id": "correctness",
            "score": 1,
            "threshold": 1,
            "status": "pass",
            "judge_model_version": None,
            "judge_prompt_version": None,
            "supporting_references": ["fixture"],
        },
        "metric_contribution": {
            "metric_id": "fixture-rate",
            "numerator": 1,
            "denominator": 2,
            "unit": "count",
            "definition_version": "v1",
            "cost_component_id": None,
        },
    }[kind]
    document["payload"] = values | payload
    return validate_event(document, RECEIVED)


def declaration(tasks=("a", "b")):
    document = json.loads((EXAMPLES / "run-declaration-v1.json").read_text())
    document["expected_task_ids"] = list(tasks)
    document["expected_task_count"] = len(tasks)
    document["evaluation_suites"] = []
    from touchstone_platform.contracts import EventIdentity, canonical_sha256

    return ValidatedDeclaration(
        EventIdentity(
            document["tenant_id"], document["workflow_id"], document["run_id"], document["event_id"]
        ),
        "trace",
        "span",
        RECEIVED,
        canonical_sha256(document),
        json.dumps(document),
    )


def test_stage_persists_cutoff_accept_reject_and_decimal_values(tmp_path):
    target = tmp_path / "snapshot.duckdb"
    events = [
        event("provider_usage", "a", cost_amount="0.1"),
        event("provider_usage", "b", cost_amount="0.2"),
        RejectedEvent(
            RECEIVED,
            "trace",
            "span",
            "touchstone.measurement.outcome",
            "invalid envelope",
            "tenant-fixture-a",
        ),
    ]
    receipt = build_snapshot(
        events, [declaration()], target, through=datetime(2026, 9, 26, 12, tzinfo=UTC)
    )

    assert receipt.cutoff == "2026-09-26T12:00:00+00:00"
    assert (
        receipt.accepted_measurements,
        receipt.accepted_declarations,
        receipt.rejected_count,
    ) == (2, 1, 1)
    with duckdb.connect(str(target), read_only=True) as connection:
        costs = connection.execute(
            "select cost_amount from raw_measurements order by event_id"
        ).fetchall()
        assert sum((cost[0] for cost in costs), Decimal()) == Decimal("0.3")
        assert connection.execute("select count(*) from raw_rejections").fetchone()[0] == 1


@pytest.mark.parametrize("bad_amount", ["0.1234567890123", "100000000000000000000000000", "NaN"])
def test_stage_rejects_money_outside_decimal_38_12(tmp_path, bad_amount):
    if bad_amount == "NaN":
        with pytest.raises(ValidationError):
            event("provider_usage", cost_amount=bad_amount)
        return
    item = event("provider_usage", cost_amount=bad_amount)
    receipt = build_snapshot(
        [item],
        [declaration()],
        tmp_path / "snapshot.duckdb",
        through=datetime(2026, 9, 26, 12, tzinfo=UTC),
    )
    assert receipt.accepted_measurements == 0
    assert receipt.rejected_count == 1


def test_stage_preserves_conflicting_identity_for_incomplete_mart(tmp_path):
    first = event("provider_usage", cost_amount="0.1")
    conflict = event("provider_usage", cost_amount="0.2")
    build_snapshot(
        [first, first, conflict],
        [declaration()],
        tmp_path / "snapshot.duckdb",
        through=datetime(2026, 9, 26, 12, tzinfo=UTC),
    )
    with duckdb.connect(str(tmp_path / "snapshot.duckdb"), read_only=True) as connection:
        assert connection.execute("select count(*) from raw_measurements").fetchone()[0] == 3
        assert (
            connection.execute(
                "select count(distinct content_sha256) from raw_measurements"
            ).fetchone()[0]
            == 2
        )


def test_cpst_divides_governed_components_with_decimal_precision():
    assert exact_cpst(
        Decimal("0.458940"), Decimal("96"), Decimal("7225.337"), 892, True
    ) == Decimal("8.208291412556053811659192825")
    assert exact_cpst(Decimal("0.3"), Decimal("4"), Decimal("6"), 1, True) == Decimal("10.3")
    assert exact_cpst(Decimal("0"), Decimal("0"), Decimal("0"), 0, True) is None
    assert exact_cpst(None, Decimal("0"), Decimal("0"), 1, True) is None
    assert exact_cpst(Decimal("0.3"), Decimal("4"), Decimal("6"), 1, False) is None

from __future__ import annotations

import importlib
import json
from decimal import Decimal

import psycopg
import pytest
from reckoner.cli import main
from reckoner.contracts import validate_document
from reckoner.storage.postgres import PostgresRepository
from test_runner import FakeProvider, _create_four_task_run


@pytest.mark.parametrize(
    "outcome,label,correct,review,error",
    [
        ("auto-approve", "legitimate", True, "0", "0"),
        ("auto-approve", "fraud", False, "0", "100"),
        ("auto-decline", "legitimate", False, "0", "30"),
        ("auto-decline", "fraud", True, "0", "0"),
        ("escalate", "legitimate", True, "4", "0"),
        ("escalate", "fraud", True, "4", "0"),
    ],
)
def test_cost_matrix(outcome, label, correct, review, error):
    evaluate_case = importlib.import_module("reckoner.baseline.evaluate").evaluate_case

    result = evaluate_case(outcome, label, 10000, Decimal("4"), Decimal("0.30"))

    assert result["status"] == "observed"
    assert result["correct"] is correct
    assert Decimal(result["review_cost"]) == Decimal(review)
    assert Decimal(result["error_cost"]) == Decimal(error)
    assert result["currency"] == "USD"
    assert result["evaluator_version"] == "reckoner-cost-v1"


@pytest.mark.parametrize("status", ["failed", "pending"])
def test_missing_outcome_never_invents_correctness_or_cost(status):
    evaluate_case = importlib.import_module("reckoner.baseline.evaluate").evaluate_case

    result = evaluate_case(None, "legitimate", 10000, Decimal("4"), Decimal("0.30"), status)

    assert result == {
        "status": status,
        "correct": None,
        "review_cost": None,
        "error_cost": None,
        "currency": None,
        "outcome_version": "reckoner-outcome-v1",
        "evaluator_version": "reckoner-cost-v1",
    }


def test_false_decline_loss_retains_sub_cent_decimal_precision():
    evaluate_case = importlib.import_module("reckoner.baseline.evaluate").evaluate_case

    result = evaluate_case("auto-decline", "legitimate", 1, Decimal("4"), Decimal("0.30"))

    assert result["error_cost"] == "0.003"


@pytest.mark.integration
def test_evaluate_run_is_idempotent_and_evaluator_evidence_is_hidden_from_runner(pg):
    evaluate = importlib.import_module("reckoner.baseline.evaluate")
    runner = importlib.import_module("reckoner.baseline.runner")
    _create_four_task_run(pg, "evaluate-four")
    provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, "evaluate-four")
        runner.execute_run(repo, provider, "evaluate-four")

    with PostgresRepository(pg.evaluator_dsn) as repo:
        first = evaluate.evaluate_run(repo, "evaluate-four")
        second = evaluate.evaluate_run(repo, "evaluate-four")
    assert first == second
    assert first == {"run_id": "evaluate-four", "evaluated": 4, "errors": 0}

    with psycopg.connect(pg.evaluator_dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM reckoner.evaluations WHERE run_id = %s",
                ("evaluate-four",),
            ).fetchone()[0]
            == 4
        )
        payloads = connection.execute(
            "SELECT payload FROM reckoner.evaluator_telemetry_outbox WHERE run_id = %s",
            ("evaluate-four",),
        ).fetchall()
        assert len(payloads) == 4
        assert all(b'"label"' not in bytes(row[0]) for row in payloads)
        protobuf = importlib.import_module(
            "opentelemetry.proto.collector.trace.v1.trace_service_pb2"
        )
        measurement_schema = importlib.import_module("reckoner.telemetry.events").MEASUREMENT_SCHEMA
        for payload in payloads:
            request = protobuf.ExportTraceServiceRequest()
            request.ParseFromString(bytes(payload[0]))
            spans = [
                span
                for resource in request.resource_spans
                for scope in resource.scope_spans
                for span in scope.spans
            ]
            assert len(spans) == 1
            assert spans[0].parent_span_id
            documents = [
                json.loads(
                    next(
                        attribute.value.string_value
                        for attribute in event.attributes
                        if attribute.key == "touchstone.measurement.json"
                    )
                )
                for event in spans[0].events
            ]
            for document in documents:
                validate_document(document, measurement_schema)
            event_kinds = [document["event_kind"] for document in documents]
            assert event_kinds.count("outcome") == 1
            assert event_kinds.count("evaluation") == 2
            assert event_kinds.count("metric_contribution") == 5

    with psycopg.connect(pg.runner_dsn) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "SELECT * FROM reckoner.evaluations WHERE run_id = %s", ("evaluate-four",)
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "SELECT * FROM reckoner.evaluator_telemetry_outbox WHERE run_id = %s",
                ("evaluate-four",),
            )


@pytest.mark.integration
def test_evaluator_exports_its_own_evidence_without_runner_or_owner_access(pg, tmp_path, capsys):
    evaluate = importlib.import_module("reckoner.baseline.evaluate")
    runner = importlib.import_module("reckoner.baseline.runner")
    _create_four_task_run(pg, "evaluation-export")
    with PostgresRepository(pg.runner_dsn) as repo:
        provider = FakeProvider()
        runner.preflight(repo, provider, "evaluation-export")
        runner.execute_run(repo, provider, "evaluation-export")
    with PostgresRepository(pg.evaluator_dsn) as repo:
        evaluate.evaluate_run(repo, "evaluation-export")

    env_file = tmp_path / "evaluator.env"
    env_file.write_text(f"RECKONER_EVALUATOR_DSN={pg.evaluator_dsn}\n")
    output = tmp_path / "evaluation-otlp"

    assert (
        main(
            [
                "export-evaluations",
                "--env-file",
                str(env_file),
                "--run-id",
                "evaluation-export",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["request_count"] == 4
    assert (output / "manifest.json").is_file()
    with psycopg.connect(pg.evaluator_dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM reckoner.evaluator_telemetry_outbox "
                "WHERE run_id = %s AND status = 'exported'",
                ("evaluation-export",),
            ).fetchone()[0]
            == 4
        )


@pytest.mark.integration
def test_cli_report_uses_evaluator_role_and_writes_linked_deterministic_evidence(
    pg, tmp_path, capsys
):
    evaluate = importlib.import_module("reckoner.baseline.evaluate")
    runner = importlib.import_module("reckoner.baseline.runner")
    _create_four_task_run(pg, "report-four")
    with PostgresRepository(pg.runner_dsn) as repo:
        provider = FakeProvider()
        runner.preflight(repo, provider, "report-four")
        runner.execute_run(repo, provider, "report-four")
    with PostgresRepository(pg.evaluator_dsn) as repo:
        evaluate.evaluate_run(repo, "report-four")

    env_file = tmp_path / "evaluator.env"
    env_file.write_text(f"RECKONER_EVALUATOR_DSN={pg.evaluator_dsn}\n")
    output = tmp_path / "report"
    command = [
        "report",
        "--env-file",
        str(env_file),
        "--run-id",
        "report-four",
        "--output",
        str(output),
    ]

    assert main(command) == 0
    assert json.loads(capsys.readouterr().out) == {
        "output": str(output),
        "status": "reported",
    }
    first = (output / "report.json").read_bytes()
    report = json.loads(first)
    for identifier in (
        "config_id",
        "bundle_id",
        "prompt_version",
        "model",
        "code_revision",
    ):
        assert report["run"][identifier]
    assert set(report["run"]["cohort_ids"]) == {"tenant-a", "tenant-b"}
    assert {item["producer"] for item in report["exports"]} == {"evaluator", "runner"}
    assert all(item["event_id"] and item["task_id"] for item in report["exports"])
    assert main(command) == 0
    capsys.readouterr()
    assert (output / "report.json").read_bytes() == first

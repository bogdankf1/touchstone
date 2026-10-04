"""Dry-run protocol execution and experiment verification, without keys or network.

Every provider response here comes from an explicit deterministic fixture
transport (`httpx.MockTransport`), labelled `execution_kind="fixture"`. Nothing in
this module is Jev/Anthropic output or measured evidence.
"""

from copy import deepcopy
from decimal import Decimal

import httpx
import psycopg
import pytest
from reckoner.contracts import content_id

# --- offline: verification refuses false complete/pass reports --------------------


def facts(**changes):
    """Fabricated execution facts for a 2-case scoring protocol."""
    calls = [
        {
            "call_id": f"call-{n}",
            "attempt_status": "responded",
            "billing_status": "settled",
            "cost": "0.000084",
            "price_table_id": "p" * 64,
            "latency_source": "live",
        }
        for n in range(2)
    ]
    run = {
        "protocol_sha256": "s" * 64,
        "execution_kind": "fixture",
        "stopped": False,
        "stop_reason": None,
        "tasks": [
            {
                "tenant_id": "tenant-a",
                "run_id": "run-a",
                "task_id": f"task-{n}",
                "transaction_id": f"tx-{n}",
                "evidence_id": f"e-{n}",
                "calibration_id": None,
                "status": "scored",
                "note_status": None,
                "calls": [calls[n]],
            }
            for n in range(2)
        ],
    }
    run.update(changes)
    return run


def protocol_fixture():
    return {
        "protocol_sha256": "s" * 64,
        "telemetry_mode": "scoring-only",
        "prices": {"price_table_version": "p" * 64},
        "cases": [
            {
                "tenant_id": "tenant-a",
                "run_id": "run-a",
                "task_id": f"task-{n}",
                "transaction_id": f"tx-{n}",
                "evidence_id": f"e-{n}",
                "evidence_mode": "relational",
            }
            for n in range(2)
        ],
    }


def claimed(**changes):
    report = {
        "status": "complete",
        "passed": True,
        "attempts": 2,
        "latency_source": "live",
        "calibration_id": None,
    }
    report.update(changes)
    return report


def verify(protocol, run, report):
    from reckoner.v1.experiment.verify import verify_experiment

    return verify_experiment(protocol, run, report)


def test_consistent_complete_execution_verifies():
    result = verify(protocol_fixture(), facts(execution_kind="measured"), claimed())
    assert result["status"] == "complete" and result["blockers"] == []
    assert result["passed"] is True and result["false_claims"] == []


def broken(name):
    run = facts()
    report = claimed()
    task, call = run["tasks"][0], run["tasks"][0]["calls"][0]
    if name == "incomplete-expected-tasks":
        run["tasks"].pop()
    elif name == "replay-latency-as-live":
        call["latency_source"] = "replay"
    elif name == "missing-price":
        call["price_table_id"] = None
    elif name == "failed-calls":
        task["status"] = "failed"
        call["attempt_status"] = "failed"
    elif name == "retries-uncounted":
        task["calls"].append({**call, "call_id": "retry"})
    elif name == "note-pending":
        task["note_status"] = "pending"
    elif name == "unknown-billing":
        call["billing_status"] = "uncertain"
        call["cost"] = None
    elif name == "over-budget-stop":
        run["stopped"] = True
        run["stop_reason"] = "provider-wide budget exceeded"
    elif name == "tenant-mismatch":
        task["tenant_id"] = "tenant-b"
    elif name == "calibration-mismatch":
        task["calibration_id"] = "c" * 64
    elif name == "evidence-mismatch":
        task["evidence_id"] = "other-evidence"
    elif name == "unpriced-cost":
        call["price_table_id"] = "q" * 64
    return run, report


BLOCKERS = [
    "incomplete-expected-tasks",
    "replay-latency-as-live",
    "missing-price",
    "failed-calls",
    "retries-uncounted",
    "note-pending",
    "unknown-billing",
    "over-budget-stop",
    "tenant-mismatch",
    "calibration-mismatch",
    "evidence-mismatch",
    "unpriced-cost",
]


@pytest.mark.parametrize("name", BLOCKERS)
def test_each_gap_blocks_a_falsely_complete_or_passing_report(name):
    run, report = broken(name)
    result = verify(protocol_fixture(), run, report)
    codes = {b["code"] for b in result["blockers"]}
    expected = "missing-price" if name == "unpriced-cost" else name
    assert expected in codes
    assert result["status"] != "complete"
    assert result["passed"] is False
    assert {"status", "passed"} <= set(result["false_claims"])


def test_honest_incomplete_report_is_not_a_false_claim():
    run, _ = broken("unknown-billing")
    result = verify(protocol_fixture(), run, claimed(status="incomplete", passed=None))
    assert result["status"] == "incomplete" and result["false_claims"] == []


def test_counted_retries_are_reported_not_hidden():
    run, _ = broken("retries-uncounted")
    result = verify(protocol_fixture(), run, claimed(attempts=3))
    assert "retries-uncounted" not in {b["code"] for b in result["blockers"]}
    assert result["attempts"] == 3


def test_fixture_execution_can_never_be_a_measured_pass():
    result = verify(protocol_fixture(), facts(), claimed(measured=True))
    assert "fixture-claimed-as-measured" in {b["code"] for b in result["blockers"]}
    assert result["passed"] is False


# --- disposable PostgreSQL: reserved protocol, fixture transport, stop-on-limit ---


def jev_body(usage=None):
    from test_v1_jev import response

    body = response()
    if usage is not None:
        body["usage"] = usage
    return body


class Recorder:
    """Deterministic fixture Jev transport recording each exact request body."""

    def __init__(self, handler):
        self.handler, self.requests = handler, []

    def __call__(self, request):
        import json

        payload = json.loads(request.content)
        self.requests.append(payload)
        return self.handler(len(self.requests), payload)


def client(recorder):
    from reckoner.v1.providers.jev import JevClient

    return JevClient("fabricated-fixture-key", transport=httpx.MockTransport(recorder))


def reserved(pg, tmp_path):
    from test_v1_protocol import experiment_scenario, fixture_approval, reserve

    draft, ledger, runs = experiment_scenario(pg, tmp_path)
    reserve(pg, draft, fixture_approval(draft))
    return draft


def execute(pg, protocol, recorder, tmp_path, **kwargs):
    from reckoner.v1.experiment.execute import execute_protocol

    kwargs.setdefault("output_dir", tmp_path / "evidence-output")
    return execute_protocol(
        protocol["protocol_sha256"],
        provider_clients={"typesafe": client(recorder)},
        dsn=pg.runner_dsn,
        execution_kind="fixture",
        **kwargs,
    )


def no_sleep(monkeypatch):
    monkeypatch.setattr("reckoner.v1.storage.attempts.time.sleep", lambda _: None)


@pytest.mark.integration
def test_fixture_dry_run_executes_exact_requests_settles_and_verifies(pg, tmp_path, monkeypatch):
    from reckoner.v1.experiment.verify import collect_run_facts, verify_experiment
    from reckoner.v1.storage.repository import V1Repository

    no_sleep(monkeypatch)
    protocol = reserved(pg, tmp_path)
    recorder = Recorder(lambda n, payload: httpx.Response(200, json=jev_body()))
    result = execute(pg, protocol, recorder, tmp_path)
    assert result["status"] == "complete" and result["execution_kind"] == "fixture"
    assert result["stop_reason"] is None and result["closed"] is True
    assert result["telemetry"]["collected"] is True and result["telemetry"]["pending"] == []
    sent = {content_id(r) for r in recorder.requests}
    approved = {t["request_sha256"] for d in protocol["dispatch"] for t in d["tasks"]}
    assert sent == approved and len(recorder.requests) == len(protocol["cases"])
    assert Decimal(result["settled_usd"]) == Decimal("0.000084") * len(protocol["cases"])
    outputs = sorted((tmp_path / "evidence-output").rglob("*.json*"))
    assert outputs and all(oct(p.stat().st_mode)[-3:] == "600" for p in outputs)
    with V1Repository(pg.runner_dsn) as repo:
        run = collect_run_facts(repo, protocol, execution_kind="fixture")
    report = verify_experiment(
        protocol,
        run,
        {"status": "complete", "passed": None, "attempts": len(protocol["cases"])},
    )
    assert report["status"] == "complete", report["blockers"]
    assert report["passed"] is False  # fixtures never pass a measured gate
    # A repeated execution reuses persisted responses: no second request.
    again = execute(pg, protocol, recorder, tmp_path, output_dir=tmp_path / "second-output")
    assert len(recorder.requests) == len(protocol["cases"])
    assert again["status"] == "complete"


@pytest.mark.integration
def test_unrecorded_protocol_and_missing_client_dispatch_nothing(pg, tmp_path):
    from reckoner.v1.experiment.execute import execute_protocol
    from test_v1_protocol import experiment_scenario

    draft, _, _ = experiment_scenario(pg, tmp_path)
    recorder = Recorder(lambda n, payload: pytest.fail("unapproved dispatch"))
    with pytest.raises(ValueError, match="recorded approval"):
        execute(pg, draft, recorder, tmp_path)
    from test_v1_protocol import fixture_approval, reserve

    reserve(pg, draft, fixture_approval(draft))
    with pytest.raises(ValueError, match="client"):
        execute_protocol(
            draft["protocol_sha256"],
            provider_clients={},
            dsn=pg.runner_dsn,
            execution_kind="fixture",
            output_dir=tmp_path / "out",
        )
    with pytest.raises(ValueError, match="execution kind"):
        execute_protocol(
            draft["protocol_sha256"],
            provider_clients={"typesafe": client(recorder)},
            dsn=pg.runner_dsn,
            execution_kind="live-looking",
            output_dir=tmp_path / "out",
        )
    assert recorder.requests == []


@pytest.mark.integration
def test_overage_stops_dispatch_and_leaves_unexecuted_cases_incomplete(pg, tmp_path, monkeypatch):
    from reckoner.v1.experiment.verify import collect_run_facts, verify_experiment
    from reckoner.v1.storage.repository import V1Repository

    no_sleep(monkeypatch)
    protocol = reserved(pg, tmp_path)
    # Fixture reports more input tokens than the per-attempt reservation covers.
    recorder = Recorder(
        lambda n, payload: httpx.Response(
            200, json=jev_body({"input_tokens": 40000, "output_tokens": 0})
        )
    )
    result = execute(pg, protocol, recorder, tmp_path)
    assert result["status"] == "stopped"
    assert "overage" in result["stop_reason"]
    assert len(recorder.requests) == 1
    assert (
        sum(c["status"] == "not-dispatched" for c in result["cases"]) == len(protocol["cases"]) - 1
    )
    with V1Repository(pg.runner_dsn) as repo:
        run = collect_run_facts(repo, protocol, execution_kind="fixture")
    report = verify_experiment(protocol, run, {"status": "complete", "passed": True, "attempts": 1})
    codes = {b["code"] for b in report["blockers"]}
    assert {"over-budget-stop", "incomplete-expected-tasks"} <= codes
    assert report["passed"] is False and "status" in report["false_claims"]


@pytest.mark.integration
def test_ambiguous_timeout_is_uncertain_and_blocks_completion(pg, tmp_path, monkeypatch):
    from reckoner.v1.experiment.verify import collect_run_facts, verify_experiment
    from reckoner.v1.storage.repository import V1Repository

    no_sleep(monkeypatch)
    protocol = reserved(pg, tmp_path)

    def handler(n, payload):
        if n == 1:
            raise httpx.ReadTimeout("fabricated ambiguous timeout")
        return httpx.Response(200, json=jev_body())

    recorder = Recorder(handler)
    result = execute(pg, protocol, recorder, tmp_path)
    assert result["status"] == "incomplete"
    assert result["uncertain_calls"] == 1
    with V1Repository(pg.runner_dsn) as repo:
        run = collect_run_facts(repo, protocol, execution_kind="fixture")
    report = verify_experiment(
        protocol, run, {"status": "complete", "passed": None, "attempts": len(recorder.requests)}
    )
    assert "unknown-billing" in {b["code"] for b in report["blockers"]}


@pytest.mark.integration
def test_execution_never_dispatches_a_changed_protocol_body(pg, tmp_path):
    protocol = reserved(pg, tmp_path)
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                "UPDATE reckoner.v1_experiment_protocols SET document=document || "
                '\'{"usd_cap": "9"}\'::jsonb'
            )
    tampered = deepcopy(protocol)
    tampered["protocol_sha256"] = "0" * 64
    recorder = Recorder(lambda n, payload: pytest.fail("tampered dispatch"))
    with pytest.raises(ValueError, match="recorded approval"):
        execute(pg, tampered, recorder, tmp_path)


@pytest.mark.integration
def test_final_workflow_protocol_decides_and_keeps_pending_notes_incomplete(
    pg, tmp_path, monkeypatch
):
    from reckoner.v1.experiment.verify import collect_run_facts, verify_experiment
    from reckoner.v1.storage.repository import V1Repository
    from test_v1_protocol import experiment_scenario, fixture_approval, reserve

    no_sleep(monkeypatch)
    draft, _, _ = experiment_scenario(pg, tmp_path, purpose="final")
    assert draft["telemetry_mode"] == "workflow"
    reserve(pg, draft, fixture_approval(draft))
    # Fixture usage must stay inside the measured bound, or overage stops dispatch.
    body = jev_body({"input_tokens": 100, "output_tokens": 0})
    body["answers"]["risk"]["probabilities"] = {"fraud": 0.4, "legitimate": 0.6}
    recorder = Recorder(lambda n, payload: httpx.Response(200, json=body))
    with pytest.raises(ValueError, match="data kind"):
        execute(pg, draft, recorder, tmp_path)
    assert recorder.requests == []
    result = execute(
        pg, draft, recorder, tmp_path, data_kind="fabricated", output_dir=tmp_path / "final"
    )
    assert result["status"] == "complete", (result["stop_reason"], result["cases"])
    assert {c["outcome"] for c in result["cases"]} == {"escalate"}
    with V1Repository(pg.runner_dsn) as repo:
        run = collect_run_facts(repo, draft, execution_kind="fixture")
    report = verify_experiment(
        draft, run, {"status": "complete", "passed": True, "attempts": len(draft["cases"])}
    )
    assert "note-pending" in {b["code"] for b in report["blockers"]}
    assert report["passed"] is False and set(report["false_claims"]) == {"status", "passed"}

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


# --- future-run defaults: the active configuration is frozen at declaration -------


def saved_configuration(pg, margin):
    from reckoner.v1.storage.configurations import create_configuration
    from reckoner.v1.storage.repository import V1Repository
    from test_v1_config_api import candidate

    body = candidate()
    body["thresholds"]["margin_rate"] = margin
    with V1Repository(pg.api_dsn) as api:
        return create_configuration(api, body)["configuration"]["config_id"]


def activate(pg, config_id, version, key):
    from reckoner.v1.storage.configurations import activate_configuration
    from reckoner.v1.storage.repository import V1Repository

    with V1Repository(pg.api_dsn) as api:
        return activate_configuration(
            api,
            tenant_id="tenant-a",
            config_id=config_id,
            expected_version=version,
            idempotency_key=key,
        )


def declaration(run_id, transaction_id, config_id=None, tenant="tenant-a"):
    return {
        "tenant_id": tenant,
        "run_id": run_id,
        "purpose": "final",
        "tasks": [{"task_id": "task-a", "transaction_id": transaction_id}],
        "dataset_version": "a" * 64,
        "cohort_version": "a" * 64,
        "code_revision": "fabricated-revision",
        "created_at": "2026-10-04T00:00:00Z",
        "config_id": config_id,
    }


@pytest.mark.integration
def test_activation_changes_only_future_run_declarations(pg):
    from reckoner.v1.experiment.runs import declare_run
    from reckoner.v1.storage.repository import V1Repository
    from test_v1_storage import setup_run

    _, manifest, _ = setup_run(pg)
    transaction = manifest["tasks"][0]["transaction_id"]
    first, second = saved_configuration(pg, "0.30"), saved_configuration(pg, "0.25")
    with V1Repository(pg.owner_dsn) as owner:
        with pytest.raises(LookupError, match="active configuration"):
            declare_run(owner, **declaration("before-activation", transaction))
    activate(pg, first, 0, "activate-a")
    with V1Repository(pg.owner_dsn) as owner:
        run_a = declare_run(owner, **declaration("run-under-a", transaction))
        assert run_a["config_source"] == "active" and run_a["activation_version"] == 1
        assert run_a["manifest"]["config_id"] == first
    activate(pg, second, 1, "activate-b")
    with V1Repository(pg.owner_dsn) as owner:
        run_b = declare_run(owner, **declaration("run-under-b", transaction))
        assert run_b["manifest"]["config_id"] == second
        # An existing run keeps its frozen configuration; the pointer is not reread.
        again = declare_run(owner, **declaration("run-under-a", transaction))
        assert again["manifest"] == run_a["manifest"] and again["config_source"] == "declared"
        explicit = declare_run(owner, **declaration("explicit-a", transaction, config_id=first))
        assert explicit["manifest"]["config_id"] == first
        assert explicit["config_source"] == "explicit"
        with pytest.raises(ValueError, match="frozen"):
            declare_run(owner, **declaration("run-under-a", transaction, config_id=second))
        stored = owner._connection.execute(
            "SELECT run_id, config_id FROM reckoner.v1_runs WHERE run_id LIKE 'run-under-%' "
            "ORDER BY run_id"
        ).fetchall()
    assert [(r["run_id"], r["config_id"]) for r in stored] == [
        ("run-under-a", first),
        ("run-under-b", second),
    ]


# --- calibration export: privileged evaluator joins of authentic scored runs -----


def calibration_case_transactions(pg, count=4):
    """Fabricated 2018 transactions (simulated fixture rows), resolved before 2019."""
    from reckoner.v1.storage.repository import V1Repository
    from test_v1_storage import setup_run

    setup_run(pg)
    with V1Repository(pg.owner_dsn) as owner:
        template = owner._connection.execute(
            "SELECT document FROM reckoner.transactions WHERE tenant_id='tenant-a' LIMIT 1"
        ).fetchone()["document"]
        docs = []
        for n in range(count):
            tx = dict(template)
            tx["transaction_id"] = f"fabricated-calibration-{n}"
            tx["occurred_at"] = f"2018-06-0{n + 1}T12:00:00Z"
            tx["account_id"] = f"fabricated-user-{n % 2}"
            owner._connection.execute(
                "INSERT INTO reckoner.transactions VALUES (%s,%s,%s)",
                ("tenant-a", tx["transaction_id"], psycopg.types.json.Jsonb(tx)),
            )
            docs.append(tx)
    return docs


def scored_validation(pg, tmp_path, monkeypatch):
    from test_v1_protocol import experiment_scenario, fixture_approval, reserve

    no_sleep(monkeypatch)
    docs = calibration_case_transactions(pg)
    draft, _, runs = experiment_scenario(
        pg, tmp_path, purpose="validation", per_tenant=(4, 0), transactions=docs, seed=False
    )
    reserve(pg, draft, fixture_approval(draft))
    probabilities = iter([0.7, 0.2, 0.6, 0.1])

    def handler(n, payload):
        fraud = next(probabilities)
        body = jev_body({"input_tokens": 100, "output_tokens": 0})
        body["answers"]["risk"]["probabilities"] = {"fraud": fraud, "legitimate": 1 - fraud}
        body["answers"]["risk"]["choice"] = "fraud" if fraud > 0.5 else "legitimate"
        return httpx.Response(200, json=body)

    result = execute(pg, draft, Recorder(handler), tmp_path)
    assert result["status"] == "complete", result["stop_reason"]
    return draft, docs


def frozen_sample(docs, labels=("fraud", "legitimate", "fraud", "legitimate")):
    from decimal import Decimal as D

    runtime = [dict(d) for d in docs]
    oracle = [
        {
            "schema_version": "oracle-v1",
            "tenant_id": d["tenant_id"],
            "transaction_id": d["transaction_id"],
            "label": label,
            "oracle_version": "cctd-label-v1",
        }
        for d, label in zip(docs, labels, strict=True)
    ]
    counts = {label: labels.count(label) for label in ("fraud", "legitimate")}
    population = {"fraud": 6, "legitimate": 40}
    sample = {
        "purpose": "validation",
        "year": 2018,
        "sample_id": "5" * 64,
        "selected_transaction_ids": sorted(d["transaction_id"] for d in docs),
        "strata": {
            label: {
                "N_h": population[label],
                "n_h": counts[label],
                "weight": str(D(population[label]) / D(counts[label])),
            }
            for label in counts
        },
    }
    return {"sample": sample, "runtime": runtime, "oracle": oracle, "bundle_id": "6" * 64}


def export(pg, draft, frozen, **overrides):
    from reckoner.v1.experiment.calibration import export_calibration_rows
    from reckoner.v1.storage.repository import V1Repository

    with V1Repository(pg.owner_dsn) as owner:
        return export_calibration_rows(
            owner,
            frozen=frozen,
            protocol=draft,
            data_kind=overrides.pop("data_kind", "fabricated"),
            **overrides,
        )


@pytest.mark.integration
def test_export_joins_scored_protocol_cases_to_the_frozen_sample(pg, tmp_path, monkeypatch):
    from reckoner.v1.calibration.fit import validate_sample

    draft, docs = scored_validation(pg, tmp_path, monkeypatch)
    exported = export(pg, draft, frozen_sample(docs))
    rows = exported["rows"]
    context, strata = validate_sample(rows, "validation")
    assert context["evidence_mode"] == "relational" and context["data_kind"] == "fabricated"
    assert context["scorer"] == {
        "provider": "typesafe",
        "model": "jev-1.13.0",
        "question_version": "binary-v1",
    }
    assert [r["probability"] for r in rows] == ["0.7", "0.2", "0.6", "0.1"]
    assert {r["label"] for r in rows} == {0, 1}
    assert exported["provenance"]["protocol_sha256"] == draft["protocol_sha256"]
    assert exported["provenance"]["sample_id"] == "5" * 64
    assert len(exported["provenance"]["score_call_ids_sha256"]) == 64
    assert "oracle" not in str(exported["provenance"]).lower().replace("oracle_version", "")


@pytest.mark.integration
@pytest.mark.parametrize(
    "defect",
    ["missing", "extra", "duplicate", "tenant", "cutoff", "model", "mode", "label"],
)
def test_export_refuses_unauthentic_or_incomplete_membership(pg, tmp_path, monkeypatch, defect):
    draft, docs = scored_validation(pg, tmp_path, monkeypatch)
    frozen = frozen_sample(docs)
    overrides = {}
    if defect == "missing":
        extra = dict(docs[0], transaction_id="unscored-sample-member")
        frozen["runtime"].append(extra)
        frozen["oracle"].append({**frozen["oracle"][0], "transaction_id": "unscored-sample-member"})
        frozen["sample"]["selected_transaction_ids"].append("unscored-sample-member")
    elif defect == "extra":
        frozen["runtime"].pop()
        frozen["oracle"].pop()
        frozen["sample"]["selected_transaction_ids"].remove(docs[-1]["transaction_id"])
    elif defect == "duplicate":
        frozen["oracle"].append(dict(frozen["oracle"][0]))
    elif defect == "tenant":
        frozen["oracle"][0]["tenant_id"] = "tenant-b"
    elif defect == "cutoff":
        frozen["sample"]["year"] = 2017
    elif defect == "model":
        overrides["expected_scorer"] = {
            "provider": "typesafe",
            "model": "jev-1.14.0",
            "question_version": "binary-v1",
        }
    elif defect == "mode":
        overrides["evidence_mode"] = "gds-augmented"
    elif defect == "label":
        frozen["oracle"][0]["label"] = "unknown"
    reasons = {
        "missing": "membership",
        "extra": "membership",
        "duplicate": "duplicate oracle",
        "tenant": "membership",
        "cutoff": "cutoff",
        "model": "scorer",
        "mode": "evidence mode",
        "label": "label",
    }
    with pytest.raises(ValueError, match=reasons[defect]):
        export(pg, draft, frozen, **overrides)


def test_selected_artifact_registration_requires_a_selected_report(tmp_path):
    from reckoner.v1.experiment.calibration import register_selected

    (tmp_path / "selected-artifact.json").write_text("null")
    with pytest.raises(ValueError, match="raw scores retained"):
        register_selected(object(), report_dir=tmp_path, tenants=["tenant-a"], context={})

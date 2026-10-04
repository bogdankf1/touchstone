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
    monkeypatch.setattr(
        "reckoner.v1.storage.attempts.time",
        __import__("types").SimpleNamespace(sleep=lambda _: None),
    )  # local to the scorer: patching time.sleep globally makes library threads spin


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
        run = collect_run_facts(repo, protocol)
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
        run = collect_run_facts(repo, protocol)
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
        run = collect_run_facts(repo, protocol)
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
        run = collect_run_facts(repo, draft)
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


def calibration_case_transactions(pg, count=4, year=2018, seed=True):
    """Fabricated transactions (simulated fixture rows), resolved before the next year."""
    from reckoner.v1.storage.repository import V1Repository
    from test_v1_storage import setup_run

    if seed:
        setup_run(pg)
    with V1Repository(pg.owner_dsn) as owner:
        template = owner._connection.execute(
            "SELECT document FROM reckoner.transactions WHERE tenant_id='tenant-a' LIMIT 1"
        ).fetchone()["document"]
        docs = []
        for n in range(count):
            tx = dict(template)
            tx["transaction_id"] = f"fabricated-calibration-{year}-{n}"
            tx["occurred_at"] = f"{year}-06-0{n + 1}T12:00:00Z"
            tx["account_id"] = f"fabricated-user-{n % 2}"
            owner._connection.execute(
                "INSERT INTO reckoner.transactions VALUES (%s,%s,%s)",
                ("tenant-a", tx["transaction_id"], psycopg.types.json.Jsonb(tx)),
            )
            docs.append(tx)
    return docs


def scored_validation(pg, tmp_path, monkeypatch, purpose="validation", seed=True):
    from test_v1_protocol import experiment_scenario, fixture_approval, reserve

    no_sleep(monkeypatch)
    year = 2018 if purpose == "validation" else 2017
    (tmp_path / purpose).mkdir(exist_ok=True)
    docs = calibration_case_transactions(pg, year=year, seed=seed)
    draft, _, runs = experiment_scenario(
        pg,
        tmp_path / purpose,
        purpose=purpose,
        per_tenant=(4, 0),
        transactions=docs,
        seed=False,
    )
    reserve(pg, draft, fixture_approval(draft))
    probabilities = iter([0.7, 0.2, 0.6, 0.1])

    def handler(n, payload):
        fraud = next(probabilities)
        body = jev_body({"input_tokens": 100, "output_tokens": 0})
        body["answers"]["risk"]["probabilities"] = {"fraud": fraud, "legitimate": 1 - fraud}
        body["answers"]["risk"]["choice"] = "fraud" if fraud > 0.5 else "legitimate"
        return httpx.Response(200, json=body)

    result = execute(pg, draft, Recorder(handler), tmp_path, output_dir=tmp_path / f"out-{purpose}")
    assert result["status"] == "complete", result["stop_reason"]
    return draft, docs


def frozen_sample(
    docs, labels=("fraud", "legitimate", "fraud", "legitimate"), purpose="validation"
):
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
        "purpose": purpose,
        "year": 2018 if purpose == "validation" else 2017,
        "sample_id": ("5" if purpose == "validation" else "7") * 64,
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
        register_selected(
            object(), report_dir=tmp_path, tenants=["tenant-a"], context={},
            development_rows=[], validation_rows=[],
        )  # fmt: skip


# --- Anthropic legacy ledger restore: dump -> disposable staging -> copy + verify -


def staging_database(pg, label="staging"):
    """A new disposable database on the test server; never a preserved store."""
    import os
    import uuid

    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    admin = conninfo_to_dict(os.environ["RECKONER_TEST_OWNER_DSN"])
    name = f"reckoner_ledger_{label}_{uuid.uuid4().hex[:10]}"
    maintenance = make_conninfo(**{**admin, "dbname": admin.get("dbname", "postgres")})
    with psycopg.connect(maintenance, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(**{**admin, "dbname": name})
    return name, dsn, maintenance


def drop_database(maintenance, name):
    from psycopg import sql

    with psycopg.connect(maintenance, autocommit=True) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s "
            "AND pid<>pg_backend_pid()",
            (name,),
        )
        connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def seed_fabricated_ledger(dsn, bundle, *, total="0.493151", entries=1040, status="settled"):
    """Explicitly fabricated Phase-1-shaped ledger rows; never the preserved ledger."""
    import json

    from conftest import CONFIG_DIR
    from reckoner.baseline.config import load_config
    from reckoner.storage.migrate import migrate
    from reckoner.storage.postgres import PostgresRepository

    migrate(dsn)
    config = load_config(CONFIG_DIR / "baseline-v1.json", CONFIG_DIR / "anthropic-prices-v1.json")
    with PostgresRepository(dsn) as repo:
        repo.import_bundle(bundle)
        for name in ("thresholds-tenant-a-v1.json", "thresholds-tenant-b-v1.json"):
            repo.register_threshold_config(json.loads((CONFIG_DIR / name).read_text()))
        repo.create_run(
            "fabricated-phase1-ledger",
            "pilot",
            config,
            json.loads((bundle / "bundle.json").read_text())["bundle_id"],
            price=json.loads((CONFIG_DIR / "anthropic-prices-v1.json").read_text()),
        )
    each = Decimal("0.000474")
    last = Decimal(total) - each * (entries - 1)
    with psycopg.connect(dsn, autocommit=True) as connection:
        task = connection.execute(
            "SELECT tenant_id, run_id, task_id FROM reckoner.tasks "
            "WHERE run_id='fabricated-phase1-ledger' ORDER BY task_id LIMIT 1"
        ).fetchone()
        for n in range(entries):
            cost = last if n == entries - 1 else each
            call = f"fabricated-ledger-{n:04d}"
            connection.execute(
                "INSERT INTO reckoner.attempts (tenant_id,run_id,task_id,call_id,status,"
                "maximum_cost,actual_cost) VALUES (%s,%s,%s,%s,'responded',%s,%s)",
                (*task, call, Decimal("0.001"), cost),
            )
            connection.execute(
                "INSERT INTO reckoner.budget_entries (tenant_id,run_id,task_id,call_id,purpose,"
                "maximum_cost,actual_cost,usage,status,settled_at) VALUES "
                "(%s,%s,%s,%s,'pilot',%s,%s,%s,%s,now())",
                (
                    *task,
                    call,
                    Decimal("0.001"),
                    cost if status == "settled" or n else None,
                    psycopg.types.json.Jsonb({"input_tokens": 1, "output_tokens": 0}),
                    status if n == 0 else "settled",
                ),
            )


@pytest.fixture
def staging(pg):
    name, dsn, maintenance = staging_database(pg)
    try:
        yield name, dsn
    finally:
        drop_database(maintenance, name)


def target_with_overlapping_transactions(pg):
    from reckoner.storage.postgres import PostgresRepository

    with PostgresRepository(pg.owner_dsn) as repo:
        repo.import_bundle(pg.bundle)


@pytest.mark.integration
def test_ledger_copy_restores_exact_chain_and_verifies_provenance(pg, staging):
    from reckoner.v1.experiment.ledger import copy_legacy_ledger
    from reckoner.v1.storage.budget import ProviderBudget

    name, dsn = staging
    seed_fabricated_ledger(dsn, pg.bundle)
    target_with_overlapping_transactions(pg)
    receipt = copy_legacy_ledger(
        staging_dsn=dsn, target_owner_dsn=pg.owner_dsn, dump_sha256="d" * 64
    )
    assert receipt["call_count"] == 1040 and receipt["settled_cost"] == "0.493151"
    assert receipt["copied"]["budget_entries"] == 1040 and receipt["dump_sha256"] == "d" * 64
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        snapshot = ProviderBudget(owner).snapshot("anthropic")
        assert snapshot["legacy_verified"] is True
        assert snapshot["legacy_ledger_sha256"] == receipt["ledger_sha256"]
        assert Decimal(snapshot["remaining_usd"]) == Decimal("9.506849")
        sources = owner.execute(
            "SELECT document->>'dump_sha256' FROM reckoner.v1_legacy_provenance "
            "WHERE document ? 'dump_sha256'"
        ).fetchall()
    assert {s[0] for s in sources} == {"d" * 64}
    again = copy_legacy_ledger(staging_dsn=dsn, target_owner_dsn=pg.owner_dsn, dump_sha256="d" * 64)
    assert again["ledger_sha256"] == receipt["ledger_sha256"]


@pytest.mark.integration
@pytest.mark.parametrize("defect", ["total", "uncertain", "conflict", "count"])
def test_ledger_copy_refuses_wrong_totals_and_conflicts_atomically(pg, staging, defect):
    from reckoner.v1.experiment.ledger import copy_legacy_ledger

    name, dsn = staging
    seed_fabricated_ledger(
        dsn,
        pg.bundle,
        total="0.493152" if defect == "total" else "0.493151",
        status="uncertain" if defect == "uncertain" else "settled",
        entries=1039 if defect == "count" else 1040,
    )
    target_with_overlapping_transactions(pg)
    if defect == "conflict":
        with psycopg.connect(dsn) as source:
            referenced = source.execute(
                "SELECT transaction_id FROM reckoner.tasks WHERE run_id='fabricated-phase1-ledger' "
                "ORDER BY task_id LIMIT 1"
            ).fetchone()[0]
        with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
            owner.execute(
                'UPDATE reckoner.transactions SET document=document || \'{"memo":"x"}\'::jsonb '
                "WHERE transaction_id=%s",
                (referenced,),
            )
    with pytest.raises(ValueError):
        copy_legacy_ledger(staging_dsn=dsn, target_owner_dsn=pg.owner_dsn, dump_sha256="d" * 64)
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        assert owner.execute("SELECT count(*) FROM reckoner.budget_entries").fetchone()[0] == 0
        assert owner.execute("SELECT count(*) FROM reckoner.runs").fetchone()[0] == 0
        assert (
            owner.execute("SELECT count(*) FROM reckoner.v1_legacy_provenance").fetchone()[0] == 0
        )


def test_restore_guards_refuse_preserved_names_and_changed_dumps(tmp_path):
    from reckoner.v1.experiment.ledger import check_databases, restore_dump

    for staging, target in (
        ("reckoner_measured", "reckoner_v1"),
        ("reckoner_ledger_staging_x", "reckoner_measured"),
        ("reckoner_v1", "reckoner_v1"),
        ("reckoner_ledger_staging_x", "reckoner_ledger_staging_x"),
    ):
        with pytest.raises(ValueError):
            check_databases(staging, target)
    check_databases("reckoner_ledger_staging_x", "reckoner_v1")
    dump = tmp_path / "fabricated.dump"
    dump.write_bytes(b"fabricated dump bytes")
    with pytest.raises(ValueError, match="SHA-256"):
        restore_dump(
            dump,
            expected_sha256="0" * 64,
            staging_dsn="dbname=reckoner_ledger_staging_x",
            command=["false"],
        )


@pytest.mark.integration
def test_custom_dump_restores_into_disposable_staging_then_copies(pg, staging, tmp_path):
    import hashlib
    import os
    import subprocess

    from psycopg.conninfo import conninfo_to_dict
    from reckoner.v1.experiment.ledger import copy_legacy_ledger, restore_dump

    container = os.environ.get("RECKONER_TEST_PG_CONTAINER")
    if not container:
        pytest.skip("needs RECKONER_TEST_PG_CONTAINER for container pg_dump/pg_restore")
    source_name, source_dsn = staging
    seed_fabricated_ledger(source_dsn, pg.bundle)
    dump = tmp_path / "fabricated-ledger.dump"
    with dump.open("wb") as handle:
        subprocess.run(
            ["docker", "exec", container, "pg_dump", "-U", "postgres", "-Fc", "-d", source_name],
            stdout=handle,
            check=True,
        )
    digest = hashlib.sha256(dump.read_bytes()).hexdigest()
    name, dsn, maintenance = staging_database(pg, label="staging")
    try:
        receipt = restore_dump(
            dump,
            expected_sha256=digest,
            staging_dsn=dsn,
            command=[
                "docker",
                "exec",
                "-i",
                container,
                "pg_restore",
                "-U",
                "postgres",
                "--no-owner",
                "--no-privileges",
                "-d",
                conninfo_to_dict(dsn)["dbname"],
            ],
        )
        assert receipt["dump_sha256"] == digest
        target_with_overlapping_transactions(pg)
        copied = copy_legacy_ledger(
            staging_dsn=dsn, target_owner_dsn=pg.owner_dsn, dump_sha256=digest
        )
        assert copied["call_count"] == 1040 and copied["dump_sha256"] == digest
    finally:
        drop_database(maintenance, name)


# --- fix round 1: recorded transport label, preflight-all, explicit errors -------


@pytest.mark.integration
def test_execution_kind_is_recorded_once_and_verify_reads_it(pg, tmp_path, monkeypatch):
    import json

    from reckoner.cli import _parser
    from reckoner.v1.cli import execute as v1
    from reckoner.v1.experiment.execute import execute_protocol

    no_sleep(monkeypatch)
    protocol = reserved(pg, tmp_path)
    recorder = Recorder(lambda n, payload: httpx.Response(200, json=jev_body()))
    execute(pg, protocol, recorder, tmp_path)
    with pytest.raises(ValueError, match="already executed as fixture"):
        execute_protocol(
            protocol["protocol_sha256"],
            provider_clients={"typesafe": client(recorder)},
            dsn=pg.runner_dsn,
            execution_kind="measured",
            output_dir=tmp_path / "relabelled",
        )
    env = tmp_path / "runner.env"
    env.write_text(f"RECKONER_RUNNER_DSN={pg.runner_dsn}\n")
    report = tmp_path / "claim.json"
    report.write_text(
        json.dumps(
            {
                "status": "complete",
                "passed": True,
                "measured": True,
                "attempts": len(protocol["cases"]),
            }
        )
    )
    result = v1(
        _parser().parse_args(
            ["v1", "protocol", "verify", "--protocol-sha256", protocol["protocol_sha256"],
             "--report", str(report), "--env-file", str(env)]
        )
    )  # fmt: skip
    assert result["execution_kind"] == "fixture" and result["passed"] is False
    assert "fixture-claimed-as-measured" in {b["code"] for b in result["blockers"]}
    with pytest.raises(ValueError, match="no recorded approval"):
        v1(
            _parser().parse_args(
                ["v1", "protocol", "verify", "--protocol-sha256", "0" * 64,
                 "--report", str(report), "--env-file", str(env)]
            )
        )  # fmt: skip


@pytest.mark.integration
def test_workflow_cases_are_all_preflighted_before_the_first_dispatch(pg, tmp_path, monkeypatch):
    import reckoner.v1.storage.attempts as attempts
    from test_v1_protocol import experiment_scenario, fixture_approval, reserve

    no_sleep(monkeypatch)
    draft, _, _ = experiment_scenario(pg, tmp_path, purpose="final")
    reserve(pg, draft, fixture_approval(draft))
    real, seen = attempts._preflight, []

    def failing(repo, task, evidence, protocol):
        seen.append(task["task_id"])
        if len(seen) == len(draft["cases"]):
            raise ValueError("fabricated preflight refusal on the last case")
        return real(repo, task, evidence, protocol)

    monkeypatch.setattr(attempts, "_preflight", failing)
    recorder = Recorder(lambda n, payload: pytest.fail("dispatch before full preflight"))
    with pytest.raises(ValueError, match="preflight refusal"):
        execute(pg, draft, recorder, tmp_path, data_kind="fabricated")
    assert recorder.requests == []


@pytest.mark.integration
def test_a_non_budget_error_keeps_partial_outputs_and_open_state(pg, tmp_path, monkeypatch):
    import json

    no_sleep(monkeypatch)
    protocol = reserved(pg, tmp_path)

    def handler(n, payload):
        if n == 2:
            raise RuntimeError("fabricated transport crash")
        return httpx.Response(200, json=jev_body())

    with pytest.raises(RuntimeError, match="transport crash"):
        execute(pg, protocol, Recorder(handler), tmp_path)
    summary = json.loads((tmp_path / "evidence-output" / "summary.json").read_text())
    assert summary["status"] == "error" and summary["closed"] is False
    assert "RuntimeError" in summary["error"]
    statuses = [c["status"] for c in summary["cases"]]
    assert statuses[:2] == ["responded", "error"] and set(statuses[2:]) <= {"not-dispatched"}
    lines = (tmp_path / "evidence-output" / "calls.jsonl").read_text().splitlines()
    assert len(lines) == 2  # the crashed call stays reserved, without a response
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        assert (
            owner.execute("SELECT count(*) FROM reckoner.v1_protocol_closures").fetchone()[0] == 0
        )
    # Resume: the crashed call becomes uncertain and is never re-sent; only the case
    # that was never dispatched is sent once.
    resumed = Recorder(lambda n, payload: httpx.Response(200, json=jev_body()))
    again = execute(pg, protocol, resumed, tmp_path, output_dir=tmp_path / "resumed")
    statuses = [c["status"] for c in again["cases"]]
    assert statuses[:2] == ["responded", "uncertain"]
    assert len(resumed.requests) == len(protocol["cases"]) - 2
    crashed = protocol["dispatch"][0]["tasks"][1]["request_sha256"]
    assert crashed not in {content_id(r) for r in resumed.requests}
    assert again["uncertain_calls"] == 1


def selected_for(context):
    from reckoner.v1.calibration import fit_calibration
    from test_v1_calibration import rehash, sample

    rows = sample()
    for row in rows:
        row["context"] = dict(context)
    artifact = fit_calibration(rows)
    artifact["qualification"] = {
        "status": "selected",
        "validation_id": "b" * 64,
        "raw_brier": 0.04,
        "candidate_brier": 0.001,
        "raw_log_loss": 0.2,
        "candidate_log_loss": 0.02,
    }
    return rehash(artifact)


def bound_artifact(context, development_rows, validation_rows):
    """A selected artifact whose identities are the exported rows' content IDs."""
    from reckoner.v1.calibration import fit_calibration
    from test_v1_calibration import rehash

    artifact = fit_calibration(development_rows)
    artifact["qualification"] = {
        "status": "selected",
        "validation_id": content_id(validation_rows),
        "raw_brier": 0.04,
        "candidate_brier": 0.001,
        "raw_log_loss": 0.2,
        "candidate_log_loss": 0.02,
    }
    return rehash(artifact)


def scored_pair(pg, tmp_path, monkeypatch):
    development, dev_docs = scored_validation(pg, tmp_path, monkeypatch, purpose="development")
    validation, val_docs = scored_validation(pg, tmp_path, monkeypatch, seed=False)
    samples = {
        "development": frozen_sample(dev_docs, purpose="development"),
        "validation": frozen_sample(val_docs),
    }
    return development, validation, samples


@pytest.mark.integration
def test_selected_artifact_binds_exported_rows_and_registers_per_tenant(pg, tmp_path, monkeypatch):
    import json

    from reckoner.cli import _parser
    from reckoner.v1.cli import execute as v1
    from reckoner.v1.experiment import calibration
    from reckoner.v1.storage.repository import V1Repository

    development, validation, samples = scored_pair(pg, tmp_path, monkeypatch)
    monkeypatch.setattr(calibration, "load_frozen_sample", lambda bundle, purpose: samples[purpose])
    with V1Repository(pg.owner_dsn) as owner:
        context = calibration.calibration_context(owner, validation, "fabricated")
        rows = {
            name: calibration.export_calibration_rows(
                owner, frozen=samples[name], protocol=protocol, data_kind="fabricated"
            )["rows"]
            for name, protocol in (("development", development), ("validation", validation))
        }
    artifact = bound_artifact(context, rows["development"], rows["validation"])
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    (report_dir / "selected-artifact.json").write_text(json.dumps(artifact))
    with V1Repository(pg.owner_dsn) as owner:
        with pytest.raises(ValueError, match="development rows"):
            calibration.register_selected(
                owner, report_dir=report_dir, tenants=["tenant-a"], context=context,
                development_rows=rows["validation"], validation_rows=rows["validation"],
            )  # fmt: skip
        with pytest.raises(ValueError, match="validation rows"):
            calibration.register_selected(
                owner, report_dir=report_dir, tenants=["tenant-a"], context=context,
                development_rows=rows["development"], validation_rows=rows["development"],
            )  # fmt: skip
    env = tmp_path / "owner.env"
    env.write_text(f"RECKONER_OWNER_DSN={pg.owner_dsn}\n")

    def register(validation_sha, development_sha):
        return v1(
            _parser().parse_args(
                ["v1", "protocol", "register-calibration", "--report-dir", str(report_dir),
                 "--protocol-sha256", validation_sha, "--development-protocol-sha256",
                 development_sha, "--bundle", str(tmp_path), "--data-kind", "fabricated",
                 "--tenant-id", "tenant-a", "--tenant-id", "tenant-b", "--env-file", str(env)]
            )
        )  # fmt: skip

    with pytest.raises(ValueError, match="validation-purpose"):
        register(development["protocol_sha256"], development["protocol_sha256"])
    with pytest.raises(ValueError, match="development-purpose"):
        register(validation["protocol_sha256"], validation["protocol_sha256"])
    with pytest.raises(ValueError, match="measured"):
        register(validation["protocol_sha256"], development["protocol_sha256"])
    with V1Repository(pg.owner_dsn) as owner:
        identity = calibration.register_selected(
            owner, report_dir=report_dir, tenants=["tenant-a", "tenant-b"], context=context,
            development_rows=rows["development"], validation_rows=rows["validation"],
        )  # fmt: skip
    assert identity == artifact["calibration_id"]
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        tenants = owner.execute(
            "SELECT tenant_id FROM reckoner.v1_selected_calibrations ORDER BY 1"
        ).fetchall()
    assert [t[0] for t in tenants] == ["tenant-a", "tenant-b"]


@pytest.mark.integration
def test_cli_exports_calibration_rows_for_a_recorded_protocol(pg, tmp_path, monkeypatch):

    from reckoner.cli import _parser
    from reckoner.v1.cli import execute as v1
    from reckoner.v1.experiment import calibration

    draft, docs = scored_validation(pg, tmp_path, monkeypatch)
    monkeypatch.setattr(
        calibration, "load_frozen_sample", lambda bundle, purpose: frozen_sample(docs)
    )
    env = tmp_path / "owner.env"
    env.write_text(f"RECKONER_OWNER_DSN={pg.owner_dsn}\n")
    # Rows from a fixture-transport execution can never feed calibration.
    with pytest.raises(ValueError, match="measured"):
        v1(
            _parser().parse_args(
                ["v1", "protocol", "export-calibration", "--protocol-sha256",
                 draft["protocol_sha256"], "--bundle", str(tmp_path), "--data-kind", "fabricated",
                 "--output", str(tmp_path / "rows.json"), "--env-file", str(env)]
            )
        )  # fmt: skip
    assert not (tmp_path / "rows.json").exists()
    from reckoner.v1.storage.repository import V1Repository

    with V1Repository(pg.owner_dsn) as owner:
        exported = calibration.export_calibration_rows(
            owner, frozen=frozen_sample(docs), protocol=draft, data_kind="fabricated"
        )
    assert len(exported["rows"]) == 4
    assert exported["provenance"]["rows_sha256"] == content_id(exported["rows"])


@pytest.mark.integration
def test_cli_declares_runs_from_the_active_configuration(pg, tmp_path):
    import json

    from reckoner.cli import _parser
    from reckoner.v1.cli import execute as v1
    from test_v1_storage import setup_run

    _, manifest, _ = setup_run(pg)
    first = saved_configuration(pg, "0.30")
    activate(pg, first, 0, "activate-a")
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps(manifest["tasks"]))
    env = tmp_path / "owner.env"
    env.write_text(f"RECKONER_OWNER_DSN={pg.owner_dsn}\n")
    args = ["v1", "protocol", "declare-run", "--tenant-id", "tenant-a", "--run-id", "cli-run",
            "--purpose", "final", "--tasks", str(tasks), "--dataset-version", "a" * 64,
            "--cohort-version", "a" * 64, "--code-revision", "fabricated-revision",
            "--created-at", "2026-10-04T00:00:00Z", "--env-file", str(env)]  # fmt: skip
    declared = v1(_parser().parse_args(args))
    assert declared["config_id"] == first and declared["config_source"] == "active"
    assert v1(_parser().parse_args(args))["config_source"] == "declared"


def evidence_file(tmp_path, call_id, usage, kind="provider-usage-record", name="evidence.json"):
    """A fabricated stand-in for a stored provider usage record; never real billing."""
    import json

    path = tmp_path / name
    path.write_text(
        json.dumps(
            {
                "schema_version": "reckoner-settlement-evidence-v1",
                "call_id": call_id,
                "source_kind": kind,
                "reference": "fabricated-provider-record-" + call_id[:8],
                "usage": usage,
            }
        )
    )
    return path


def timed_out_call(pg, tmp_path, monkeypatch, body=None):
    no_sleep(monkeypatch)
    protocol = reserved(pg, tmp_path)

    def handler(n, payload):
        if n == 1:
            if body is not None:
                return httpx.Response(200, json=body)
            raise httpx.ReadTimeout("fabricated ambiguous timeout")
        return httpx.Response(200, json=jev_body())

    execute(pg, protocol, Recorder(handler), tmp_path)
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        call_id = owner.execute(
            "SELECT call_id FROM reckoner.v1_settlements WHERE status='uncertain'"
        ).fetchone()[0]
    return protocol, call_id


def settle_cli(pg, tmp_path, call_id, usage, evidence=None):
    from reckoner.cli import _parser
    from reckoner.v1.cli import execute as v1

    env = tmp_path / "owner.env"
    env.write_text(f"RECKONER_OWNER_DSN={pg.owner_dsn}\n")
    args = ["v1", "protocol", "settle", "--call-id", call_id,
            "--input-tokens", str(usage["input_tokens"]),
            "--output-tokens", str(usage["output_tokens"]), "--env-file", str(env)]  # fmt: skip
    if evidence is not None:
        args += ["--evidence", str(evidence)]
    return v1(_parser().parse_args(args))


@pytest.mark.integration
def test_settle_needs_matching_provider_evidence_and_persists_it(pg, tmp_path, monkeypatch):
    import hashlib

    from reckoner.v1.experiment.ledger import reconcile_call
    from reckoner.v1.storage.budget import ProviderBudget

    protocol, call_id = timed_out_call(pg, tmp_path, monkeypatch)
    usage = {"input_tokens": 2000, "output_tokens": 0}
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        with pytest.raises(ValueError, match="evidence"):
            reconcile_call(owner, call_id, usage, evidence=None)
    with pytest.raises(SystemExit):
        settle_cli(pg, tmp_path, call_id, usage)  # --evidence is required
    for _name, path, entered in (
        ("other call", evidence_file(tmp_path, "other-call", usage, name="a.json"), usage),
        ("usage", evidence_file(tmp_path, call_id, usage, name="b.json"),
         {"input_tokens": 0, "output_tokens": 0}),
        ("kind", evidence_file(tmp_path, call_id, usage, kind="operator-estimate", name="c.json"),
         usage),
    ):  # fmt: skip
        with pytest.raises(ValueError):
            settle_cli(pg, tmp_path, call_id, entered, path)
    good = evidence_file(tmp_path, call_id, usage, name="good.json")
    settled = settle_cli(pg, tmp_path, call_id, usage, good)
    assert settled["cost"] == "0.000084" and settled["prior_state"] == "dispatched-unknown"
    with pytest.raises(ValueError, match="already settled"):
        settle_cli(pg, tmp_path, call_id, usage, good)
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        stored = owner.execute(
            "SELECT document FROM reckoner.v1_settlement_evidence WHERE call_id=%s", (call_id,)
        ).fetchone()[0]
        assert stored["document_sha256"] == hashlib.sha256(good.read_bytes()).hexdigest()
        assert stored["source_kind"] == "provider-usage-record"
        assert stored["prior_state"] == "dispatched-unknown"
        assert ProviderBudget(owner).snapshot("typesafe")["unresolved"] == []


@pytest.mark.integration
def test_settle_must_equal_persisted_response_usage(pg, tmp_path, monkeypatch):
    body = jev_body({"input_tokens": 2000, "output_tokens": 0, "unrecognised": 1})
    protocol, call_id = timed_out_call(pg, tmp_path, monkeypatch, body=body)
    other = {"input_tokens": 1500, "output_tokens": 0}
    with pytest.raises(ValueError, match="persisted response usage"):
        settle_cli(pg, tmp_path, call_id, other, evidence_file(tmp_path, call_id, other))
    usage = {"input_tokens": 2000, "output_tokens": 0}
    settled = settle_cli(
        pg, tmp_path, call_id, usage, evidence_file(tmp_path, call_id, usage, name="ok.json")
    )
    assert settled["prior_state"] == "responded-unknown-billing"


@pytest.mark.integration
def test_never_answered_call_stays_uncertain_without_evidence_and_is_never_auto_zeroed(
    pg, tmp_path
):
    from decimal import Decimal

    from reckoner.v1.experiment.ledger import reconcile_call
    from reckoner.v1.storage.budget import ProviderBudget
    from reckoner.v1.storage.repository import V1Repository
    from test_v1_budget import call

    protocol = reserved(pg, tmp_path)
    dispatch = protocol["dispatch"][0]
    task = {"tenant_id": dispatch["tenant_id"], "run_id": dispatch["run_id"],
            **{k: dispatch["tasks"][0][k] for k in ("task_id", "transaction_id")}}  # fmt: skip
    reservation = {**call(task, dispatch, "never-answered"), "request_document": {}}
    reservation["request_sha256"] = dispatch["tasks"][0]["request_sha256"]
    with V1Repository(pg.runner_dsn) as repo:
        ProviderBudget(repo._connection).reserve(
            reservation, Decimal(dispatch["attempt_maximum_usd"]), dispatch
        )
    zero = {"input_tokens": 0, "output_tokens": 0}
    zero_evidence = evidence_file(
        tmp_path, "never-answered", zero, kind="provider-request-log", name="z.json"
    )
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        with pytest.raises(ValueError, match="evidence"):
            reconcile_call(owner, "never-answered", zero, evidence=None)
        assert ProviderBudget(owner).snapshot("typesafe")["unresolved"]
    # Not yet recorded as uncertain, then still in an open envelope, then in flight.
    with pytest.raises(ValueError, match="recorded as uncertain.*execute.*close.*settle"):
        settle_cli(pg, tmp_path, "never-answered", zero, zero_evidence)
    with V1Repository(pg.runner_dsn) as repo:
        ProviderBudget(repo._connection).settle("never-answered", None, None)
    with pytest.raises(ValueError, match="close.*execute.*close.*settle"):
        settle_cli(pg, tmp_path, "never-answered", zero, zero_evidence)
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        for item in protocol["dispatch"]:
            ProviderBudget(owner).close(item["protocol_id"])
        owner.execute(
            "INSERT INTO reckoner.v1_provider_state (provider, active_call) "
            "VALUES ('typesafe','never-answered') ON CONFLICT (provider) "
            "DO UPDATE SET active_call='never-answered'"
        )
    with pytest.raises(ValueError, match="in flight.*execute.*close.*settle"):
        settle_cli(pg, tmp_path, "never-answered", zero, zero_evidence)
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        owner.execute("UPDATE reckoner.v1_provider_state SET active_call=NULL")
    with pytest.raises(ValueError):
        settle_cli(pg, tmp_path, "never-answered", zero,
                   evidence_file(tmp_path, "never-answered", {"input_tokens": 10,
                                                              "output_tokens": 0}))  # fmt: skip
    settled = settle_cli(
        pg, tmp_path, "never-answered", zero,
        evidence_file(tmp_path, "never-answered", zero, kind="provider-request-log", name="z.json"),
    )  # fmt: skip
    assert settled["prior_state"] == "never-answered" and settled["cost"] == "0"


@pytest.mark.integration
@pytest.mark.parametrize(
    "field,value",
    [
        ("telemetry_mode", "scoring-only"),
        ("dataset_version", "b" * 64),
        ("cohort_version", "b" * 64),
        ("created_at", "2026-10-05T00:00:00Z"),
        ("code_revision", "other-revision"),
    ],
)
def test_existing_run_refuses_any_changed_declaration(pg, field, value):
    from reckoner.v1.experiment.runs import declare_run
    from reckoner.v1.storage.repository import V1Repository
    from test_v1_storage import setup_run

    _, manifest, _ = setup_run(pg)
    first = saved_configuration(pg, "0.30")
    activate(pg, first, 0, "activate-a")
    base = {**declaration("frozen-run", manifest["tasks"][0]["transaction_id"]),
            "purpose": "validation"}  # fmt: skip
    with V1Repository(pg.owner_dsn) as owner:
        declare_run(owner, **base, telemetry_mode="workflow")
        changed = {**base, "telemetry_mode": "workflow", field: value}
        with pytest.raises(ValueError, match="frozen"):
            declare_run(owner, **changed)
        assert declare_run(owner, **base, telemetry_mode="workflow")["config_source"] == "declared"


# --- fix round 2: workflow verification keeps scorer failures and evidence gaps --


@pytest.mark.integration
def test_workflow_verification_blocks_scorer_failures_and_records_evidence_gaps(
    pg, tmp_path, monkeypatch
):
    from reckoner.v1.experiment.verify import collect_run_facts, verify_experiment
    from reckoner.v1.storage.repository import V1Repository
    from test_v1_protocol import experiment_scenario, fixture_approval, reserve

    no_sleep(monkeypatch)
    draft, _, _ = experiment_scenario(
        pg, tmp_path, purpose="final", per_tenant=(3, 0), unavailable={1}
    )
    reserve(pg, draft, fixture_approval(draft))

    def handler(n, payload):
        if n == 1:
            return httpx.Response(401, text="fabricated authentication failure")
        body = jev_body({"input_tokens": 100, "output_tokens": 0})
        body["answers"]["risk"]["probabilities"] = {"fraud": 0.001, "legitimate": 0.999}
        return httpx.Response(200, json=body)

    recorder = Recorder(handler)
    execute(pg, draft, recorder, tmp_path, data_kind="fabricated")
    assert len(recorder.requests) == 2  # the unavailable-evidence case is never dispatched
    with V1Repository(pg.runner_dsn) as repo:
        facts = collect_run_facts(repo, draft)
    by_task = {t["task_id"]: t for t in facts["tasks"]}
    assert by_task["task-0"]["status"] == "failed"
    assert by_task["task-0"]["degraded_reason"] == "scorer_failed"
    assert by_task["task-1"]["stages"] == [] and by_task["task-1"]["coverage_gap"] == (
        "evidence_unavailable"
    )
    assert by_task["task-2"]["status"] == "decided"
    report = verify_experiment(draft, facts, {"status": "complete", "passed": True, "attempts": 2})
    codes = {b["code"] for b in report["blockers"]}
    assert "failed-calls" in codes and "missing-stage" not in codes
    assert report["coverage_gaps"] == [["tenant-a", "final-tenant-a", "task-1"]]
    assert report["passed"] is False


# --- quality round 1: approved per-attempt maximum, coverage gaps -------------


@pytest.mark.integration
def test_uncertain_call_keeps_the_approved_per_attempt_maximum_after_close(
    pg, tmp_path, monkeypatch
):
    from reckoner.v1.storage.budget import ProviderBudget

    protocol, call_id = timed_out_call(pg, tmp_path, monkeypatch)
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        maximum = owner.execute(
            "SELECT maximum_cost FROM reckoner.v1_provider_calls WHERE call_id=%s", (call_id,)
        ).fetchone()[0]
        snapshot = ProviderBudget(owner).snapshot("typesafe")
    assert maximum == Decimal("0.002688")
    settled = Decimal("0.000084") * (len(protocol["cases"]) - 1)
    assert Decimal(snapshot["liability_usd"]) == settled + Decimal("0.002688")


def gapped_facts():
    run = facts(execution_kind="measured")
    run["tasks"][1].update(status="decided", coverage_gap="evidence_unavailable", calls=[])
    return run


def test_coverage_gaps_must_be_disclosed_and_block_a_pass_above_the_pinned_threshold():
    gap = [["tenant-a", "run-a", "task-1"]]
    unpinned = protocol_fixture()  # zero expected gaps were shown at approval
    pinned = {**unpinned, "coverage_gap_threshold": 1, "expected_coverage_gaps": gap}
    hidden = verify(pinned, gapped_facts(), claimed(attempts=1))
    assert "coverage-gap-undisclosed" in {b["code"] for b in hidden["blockers"]}
    assert hidden["passed"] is False and {"status", "passed"} <= set(hidden["false_claims"])
    disclosed = verify(pinned, gapped_facts(), claimed(attempts=1, coverage_gaps=gap))
    assert disclosed["status"] == "complete-with-gaps" and disclosed["blockers"] == []
    assert "status" in disclosed["false_claims"]  # it claimed "complete"
    honest = verify(
        pinned,
        gapped_facts(),
        claimed(attempts=1, coverage_gaps=gap, status="complete-with-gaps", passed=None),
    )
    assert honest["false_claims"] == [] and honest["passed"] is False
    tolerant = verify(
        pinned, gapped_facts(), claimed(attempts=1, coverage_gaps=gap, status="complete-with-gaps")
    )
    assert tolerant["passed"] is True and tolerant["status"] == "complete-with-gaps"
    # A gap the owner was not shown at approval time blocks a pass.
    for protocol in (
        unpinned,
        {**pinned, "expected_coverage_gaps": [["tenant-a", "run-a", "task-0"]]},
    ):
        unexpected = verify(
            protocol,
            gapped_facts(),
            claimed(attempts=1, coverage_gaps=gap, status="complete-with-gaps"),
        )
        assert "coverage-gap-unexpected" in {b["code"] for b in unexpected["blockers"]}
        assert unexpected["passed"] is False


@pytest.mark.integration
def test_a_run_with_every_case_lacking_evidence_never_passes_silently(pg, tmp_path, monkeypatch):
    from reckoner.v1.experiment.verify import collect_run_facts, verify_experiment
    from reckoner.v1.storage.repository import V1Repository
    from test_v1_protocol import experiment_scenario, fixture_approval, reserve

    no_sleep(monkeypatch)
    draft, _, _ = experiment_scenario(
        pg, tmp_path, purpose="final", per_tenant=(3, 0), unavailable={0, 1, 2}
    )
    # The published evidence already showed all three as unavailable, so the owner sees
    # exactly these cases (and their count) in the protocol before approving it.
    assert draft["coverage_gap_threshold"] == 3
    assert draft["expected_coverage_gaps"] == [
        ["tenant-a", "final-tenant-a", f"task-{n}"] for n in range(3)
    ]
    reserve(pg, draft, fixture_approval(draft))
    recorder = Recorder(lambda n, payload: pytest.fail("no case had evidence to score"))
    execute(pg, draft, recorder, tmp_path, data_kind="fabricated")
    with V1Repository(pg.runner_dsn) as repo:
        run = collect_run_facts(repo, draft)
    claim = {"status": "complete", "passed": True, "attempts": 0}
    report = verify_experiment(draft, run, claim)
    assert len(report["coverage_gaps"]) == 3
    assert "coverage-gap-undisclosed" in {b["code"] for b in report["blockers"]}
    assert report["passed"] is False and set(report["false_claims"]) == {"status", "passed"}


@pytest.mark.integration
def test_settle_reference_must_match_a_persisted_provider_request_id(pg, tmp_path):
    from decimal import Decimal

    from psycopg.types.json import Jsonb
    from reckoner.v1.storage.budget import ProviderBudget
    from reckoner.v1.storage.repository import V1Repository
    from test_v1_budget import call

    protocol = reserved(pg, tmp_path)
    dispatch = protocol["dispatch"][0]
    task = {"tenant_id": dispatch["tenant_id"], "run_id": dispatch["run_id"],
            **{k: dispatch["tasks"][0][k] for k in ("task_id", "transaction_id")}}  # fmt: skip
    reservation = {**call(task, dispatch, "with-request-id"), "request_document": {}}
    reservation["request_sha256"] = dispatch["tasks"][0]["request_sha256"]
    with V1Repository(pg.runner_dsn) as repo:
        ledger = ProviderBudget(repo._connection)
        ledger.reserve(reservation, Decimal(dispatch["attempt_maximum_usd"]), dispatch)
        ledger.settle("with-request-id", None, None)
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        # Fabricated persisted response that names its provider request identity.
        owner.execute(
            "INSERT INTO reckoner.v1_provider_responses (tenant_id,call_id,category,body,score) "
            "VALUES (%s,'with-request-id','uncertain',%s,%s)",
            (task["tenant_id"], Jsonb({"provider_request_id": "fabricated-request-7"}),
             Jsonb({"tenant_id": task["tenant_id"], "call_id": "with-request-id"})),
        )  # fmt: skip
        for item in protocol["dispatch"]:
            ProviderBudget(owner).close(item["protocol_id"])
    usage = {"input_tokens": 100, "output_tokens": 0}
    wrong = evidence_file(tmp_path, "with-request-id", usage, name="wrong.json")
    with pytest.raises(ValueError, match="provider request"):
        settle_cli(pg, tmp_path, "with-request-id", usage, wrong)
    right = tmp_path / "right.json"
    import json

    record = json.loads(wrong.read_text()) | {"reference": "fabricated-request-7"}
    right.write_text(json.dumps(record))
    assert settle_cli(pg, tmp_path, "with-request-id", usage, right)["status"] == "settled"


# --- quality round 1: execution bookkeeping and provider-key scope ------------


@pytest.mark.integration
def test_outputs_prefer_settled_billing_and_telemetry_failures_are_captured(
    pg, tmp_path, monkeypatch
):
    import json

    from reckoner.v1.experiment import execute as execution
    from reckoner.v1.storage.repository import V1Repository

    protocol, call_id = timed_out_call(pg, tmp_path, monkeypatch)
    usage = {"input_tokens": 2000, "output_tokens": 0}
    settle_cli(pg, tmp_path, call_id, usage, evidence_file(tmp_path, call_id, usage))
    with V1Repository(pg.runner_dsn) as repo:
        execution.retain_outputs(repo, protocol, execution._output(tmp_path / "after-settle"))
    records = [
        json.loads(line)
        for line in (tmp_path / "after-settle" / "calls.jsonl").read_text().splitlines()
    ]
    assert next(r for r in records if r["call_id"] == call_id)["billing_status"] == "settled"

    second = reserved_again(pg, tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("fabricated collector outage")

    monkeypatch.setattr(execution, "_telemetry", broken)
    result = execute(
        pg, second, Recorder(lambda n, p: httpx.Response(200, json=jev_body())), tmp_path,
        output_dir=tmp_path / "telemetry-down",
    )  # fmt: skip
    assert result["telemetry"]["collected"] is False and "outage" in result["telemetry"]["error"]
    assert (tmp_path / "telemetry-down" / "summary.json").exists()


def reserved_again(pg, tmp_path):
    """A second scoring protocol over new runs of the same fabricated cases."""
    from test_v1_protocol import experiment_scenario, fixture_approval, reserve

    (tmp_path / "again").mkdir(exist_ok=True)
    draft, _, _ = experiment_scenario(pg, tmp_path / "again", purpose="development", seed=False)
    reserve(pg, draft, fixture_approval(draft))
    return draft


@pytest.mark.integration
def test_existing_output_directory_refuses_before_recording_a_transport_label(pg, tmp_path):
    protocol = reserved(pg, tmp_path)
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / "previous.txt").write_text("earlier output")
    recorder = Recorder(lambda n, payload: pytest.fail("no dispatch"))
    with pytest.raises(FileExistsError):
        execute(pg, protocol, recorder, tmp_path, output_dir=taken)
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        assert (
            owner.execute("SELECT count(*) FROM reckoner.v1_protocol_executions").fetchone()[0] == 0
        )


@pytest.mark.integration
def test_output_token_overage_is_reported_as_the_stop_reason(pg, tmp_path, monkeypatch):
    from reckoner.v1.experiment.verify import collect_run_facts
    from reckoner.v1.storage.repository import V1Repository

    no_sleep(monkeypatch)
    protocol = reserved(pg, tmp_path)
    recorder = Recorder(
        lambda n, payload: httpx.Response(
            200, json=jev_body({"input_tokens": 100, "output_tokens": 2000})
        )
    )
    result = execute(pg, protocol, recorder, tmp_path)
    assert result["status"] == "stopped"
    with V1Repository(pg.runner_dsn) as repo:
        run = collect_run_facts(repo, protocol)
    assert run["stopped"] and "overage" in run["stop_reason"]


@pytest.mark.integration
def test_cli_execute_loads_only_the_protocol_providers_key(pg, tmp_path, monkeypatch):
    from reckoner.cli import _parser
    from reckoner.v1.cli import execute as v1
    from reckoner.v1.experiment import execute as execution
    from reckoner.v1.notes import provider as notes_provider

    protocol = reserved(pg, tmp_path)
    captured = {}

    def capture(protocol_id, **kwargs):
        captured.update(kwargs)
        return {"captured": True}

    def no_anthropic(*args, **kwargs):
        raise AssertionError("the Anthropic key must not be loaded for a Jev protocol")

    monkeypatch.setattr(execution, "execute_protocol", capture)
    monkeypatch.setattr(notes_provider, "NoteProvider", no_anthropic)
    env = tmp_path / "keys.env"
    env.write_text(
        f"RECKONER_RUNNER_DSN={pg.runner_dsn}\nJEV_API_KEY=fabricated-not-a-key\n"
        "ANTHROPIC_API_KEY=fabricated-not-a-key\n"
    )
    args = ["v1", "protocol", "execute", "--protocol-sha256", protocol["protocol_sha256"],
            "--output", str(tmp_path / "unused"), "--env-file", str(env)]  # fmt: skip
    assert v1(_parser().parse_args(args)) == {"captured": True}
    assert set(captured["provider_clients"]) == {"typesafe"}
    env.write_text(f"RECKONER_RUNNER_DSN={pg.runner_dsn}\nANTHROPIC_API_KEY=fabricated-not-a-key\n")
    with pytest.raises(ValueError, match="JEV_API_KEY"):
        v1(_parser().parse_args(args))


# --- final follow-up -------------------------------------------------------------


@pytest.mark.integration
def test_reconciliation_prices_at_the_recorded_protocol_price_not_todays(pg, tmp_path, monkeypatch):
    from reckoner.v1 import pricing

    protocol, call_id = timed_out_call(pg, tmp_path, monkeypatch)
    # A later published price change must not reprice an approved protocol's call.
    monkeypatch.setitem(pricing.PUBLISHED["typesafe"], "input_per_million", Decimal("0.05"))
    usage = {"input_tokens": 2000, "output_tokens": 0}
    settled = settle_cli(pg, tmp_path, call_id, usage, evidence_file(tmp_path, call_id, usage))
    assert settled["cost"] == "0.000084"


def test_paid_run_operator_guide_states_recovery_and_the_database_residual():
    from pathlib import Path

    guide = (
        Path(__file__).resolve().parents[3] / "docs/operations/reckoner-v1-paid-runs.md"
    ).read_text()
    for phrase in (
        "reckoner v1 protocol execute",
        "reckoner v1 protocol close",
        "reckoner v1 protocol settle",
        "never-answered",
        "Residual",
        "simulated",
    ):
        assert phrase in guide

"""Task-owned PostgreSQL and a fabricated legacy ledger; no real billing evidence."""

import importlib
import json
from decimal import Decimal

import psycopg
import pytest
from reckoner.contracts import content_id
from reckoner.storage.budget import BudgetLedger
from reckoner.storage.postgres import PostgresRepository
from reckoner.v1.storage.budget import ProviderBudget
from reckoner.v1.storage.repository import V1Repository
from test_v1_budget import protocol
from test_v1_note_generation import body, sample
from test_v1_storage import setup_run
from v1_fixtures import authorize, identified

pytestmark = pytest.mark.integration


def modules():
    try:
        return importlib.import_module("reckoner.v1.notes.calls"), importlib.import_module(
            "reckoner.v1.notes.lifecycle"
        )
    except ModuleNotFoundError:
        pytest.fail("durable note dispatch is not implemented")


def ledger_fixture(pg, legacy):
    with PostgresRepository(pg.runner_dsn) as repo:
        first = BudgetLedger(repo).reserve(legacy, Decimal(".493151"))
        BudgetLedger(repo).settle(
            first["call_id"], Decimal(".493151"), {"input_tokens": 1, "output_tokens": 0}
        )
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        owner.execute(
            "INSERT INTO reckoner.attempts "
            "(tenant_id,run_id,task_id,call_id,status,maximum_cost,actual_cost"
            ") SELECT tenant_id,run_id,task_id,'fabricated-note-legacy-' || "
            "n,'responded',0,0 FROM reckoner.attempts CROSS JOIN "
            "generate_series(1,1039) n WHERE call_id=%s",
            (first["call_id"],),
        )
        owner.execute(
            "INSERT INTO reckoner.budget_entries "
            "(tenant_id,run_id,task_id,call_id,purpose,status,maximum_cost,act"
            "ual_cost) SELECT "
            "tenant_id,run_id,task_id,call_id,'baseline','settled',0,0 FROM "
            "reckoner.attempts WHERE call_id LIKE 'fabricated-note-legacy-%'"
        )
        ProviderBudget(owner).verify_legacy()


class Transport:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []

    def generate(self, request):
        self.calls.append(request)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


def paid_body(content="{}"):
    return {
        **body(content),
        "requested_model": "anthropic/claude-haiku-4-5-20251001",
        "provider_request_id": "fabricated",
        "input_tokens": 20,
        "output_tokens": 10,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }


def prepared_call(pg):
    config, manifest, legacy = setup_run(pg)
    ledger_fixture(pg, legacy)
    repo = V1Repository(pg.runner_dsn)
    task = repo.task("tenant-a", manifest["run_id"], "task-a")
    request = {
        "model": config["note_model"]["model"],
        "system": "note-v1",
        "messages": [{"role": "user", "content": "simulated data"}],
        "temperature": 0,
        "max_tokens": 1000,
    }
    p = protocol(task, content_id(request), provider="anthropic", attempts=1)
    p["purpose"] = "online-note"
    identified(p, "protocol_id")
    authorize(pg.owner_dsn, p)
    return repo, task, config, request, p


def test_actual_attempt_responses_reused_after_restart(pg):
    api, _ = modules()
    repo, task, config, request, p = prepared_call(pg)
    transport = Transport([paid_body()])
    with repo:
        calls = api.BudgetedCalls(repo, transport, task, config, kind="note")
        first = calls.execute(request, stage="note-generation", protocol=p)
        second = api.BudgetedCalls(repo, transport, task, config, kind="note").execute(
            request, stage="note-generation", protocol=p
        )
        assert first == second
        assert first["billing_status"] == "settled"
        assert first["cost"] == "0.00007"
        assert len(transport.calls) == 1
        assert (
            repo._connection.execute(
                "SELECT count(*) AS n FROM reckoner.v1_settlements"
            ).fetchone()["n"]
            == 1
        )


def test_invalid_protocol_cannot_pin_or_dispatch(pg):
    api, _ = modules()
    repo, task, config, request, p = prepared_call(pg)
    p["tasks"][0]["request_sha256"] = "f" * 64
    identified(p, "protocol_id")
    transport = Transport([])
    with repo:
        with pytest.raises(ValueError):
            api.BudgetedCalls(repo, transport, task, config, kind="note").execute(
                request, stage="note-generation", protocol=p
            )
        assert (
            repo._connection.execute(
                "SELECT count(*) AS n FROM reckoner.v1_provider_calls"
            ).fetchone()["n"]
            == 0
        )
        assert transport.calls == []


def test_ambiguous_dispatch_never_retries_or_refunds(pg):
    api, _ = modules()
    repo, task, config, request, p = prepared_call(pg)
    transport = Transport([TimeoutError("secret must not persist")])
    with repo:
        calls = api.BudgetedCalls(repo, transport, task, config, kind="note")
        first = calls.execute(request, stage="note-generation", protocol=p)
        assert first["status"] == "uncertain"
        assert first["billing_status"] == "uncertain"
        assert calls.execute(request, stage="note-generation", protocol=p) == first
        assert len(transport.calls) == 1
        assert "secret" not in json.dumps(first)


def test_note_declaration_exists_after_decision_and_late_continuation(pg):
    api, lifecycle = modules()
    from reckoner.v1.notes import CONTENT_FIELDS, build_note_request
    from test_v1_graph_workflow import execute, prepared

    repo, task, evidence, settings, score_calls = prepared(pg)
    # setup_run created the baseline task used solely for this test's fabricated ledger.
    with PostgresRepository(pg.runner_dsn) as old:
        legacy = next(
            t for t in old.pending_tasks("baseline-preserved") if t["tenant_id"] == "tenant-a"
        )
    ledger_fixture(pg, legacy)
    with repo:
        decision, _ = execute(repo, task, settings)
        declared = lifecycle.note_work(repo, decision)
        assert declared["status"] == "pending"
        original = json.dumps(decision, sort_keys=True)
        score = repo._connection.execute(
            "SELECT score FROM reckoner.v1_provider_responses WHERE call_id=%s",
            (decision["call_id"],),
        ).fetchone()["score"]
        config = repo.workflow_document(
            "v1_configs", {"tenant_id": task["tenant_id"], "config_id": task["config_id"]}
        )
        note, _, _ = sample()
        from reckoner.v1.notes.validate import note_data

        supplied = note_data(evidence, score)
        fields = {key: note[key] for key in CONTENT_FIELDS}
        fields.update(
            {
                key: supplied[key]
                for key in (
                    "confidence",
                    "entity_neighbourhood",
                    "risk_indicators",
                    "comparable_cases",
                )
            }
        )
        fields["what_would_change_verdict"] = []
        request = build_note_request(decision, evidence, score, config)
        p = protocol(task, content_id(request), provider="anthropic", attempts=1)
        p["input_token_ceiling"] = config["limits"]["input_token_ceiling"]
        p["purpose"] = "online-note"
        identified(p, "protocol_id")
        authorize(pg.owner_dsn, p)
        repo.note_client = Transport([paid_body(json.dumps(fields))])
        repo.note_protocol = p
        execute(repo, task, settings)
        ready = lifecycle.note_work(repo, decision)
        assert ready["status"] == "succeeded"
        assert execute(repo, task, settings)[0] == decision
        assert len(repo.note_client.calls) == 1 and len(score_calls) == 1
        assert json.dumps(decision, sort_keys=True) == original


def test_crash_after_response_reuses_settlement_before_note_result_write(pg, monkeypatch):
    api, _ = modules()
    repo, task, config, request, p = prepared_call(pg)
    transport = Transport([paid_body()])
    with repo:
        calls = api.BudgetedCalls(repo, transport, task, config, kind="note")
        original = calls._finish

        def crashed(*args):
            original(*args)
            raise RuntimeError("worker stopped after persisted response")

        monkeypatch.setattr(calls, "_finish", crashed)
        with pytest.raises(RuntimeError):
            calls.execute(request, stage="note-generation", protocol=p)
    with V1Repository(pg.runner_dsn) as restarted:
        result = api.BudgetedCalls(restarted, transport, task, config, kind="note").execute(
            request, stage="note-generation", protocol=p
        )
        assert result["billing_status"] == "settled"
        assert len(transport.calls) == 1
        assert (
            restarted._connection.execute(
                "SELECT count(*) AS n FROM reckoner.v1_settlements"
            ).fetchone()["n"]
            == 1
        )


def test_uncertain_usage_stays_distinct_from_valid_note_content(pg):
    api, _ = modules()
    repo, task, config, request, p = prepared_call(pg)
    reply = paid_body()
    reply.pop("input_tokens")
    with repo:
        result = api.BudgetedCalls(repo, Transport([reply]), task, config, kind="note").execute(
            request, stage="note-generation", protocol=p
        )
        assert result["status"] == "responded"
        assert result["billing_status"] == "uncertain"
        assert result["cost"] is None


def test_each_ragas_stage_is_reserved_and_saved_individually(pg):
    api, _ = modules()
    from reckoner.v1.evaluation.judges import RagasFaithfulness

    # A larger pinned test configuration is needed for Ragas' full prompt templates.
    config, manifest, legacy = setup_run(pg)
    config["limits"]["input_token_ceiling"] = 16000
    identified(config, "config_id")
    manifest.update(config_id=config["config_id"], run_id="judge-run")
    identified(manifest, "experiment_id")
    with V1Repository(pg.owner_dsn) as owner:
        owner.register_config(config)
        owner.create_run(manifest, config["config_id"])
    ledger_fixture(pg, legacy)
    with V1Repository(pg.runner_dsn) as repo:
        task = repo.task("tenant-a", "judge-run", "task-a")
        responses = [
            paid_body('{"statements":["Neighbourhood unavailable."]}'),
            paid_body(
                '{"statements":[{"statement":"Neighbourhood '
                'unavailable.","reason":"Explicit.","verdict":1}]}'
            ),
        ]
        transport = Transport(responses)
        calls = api.BudgetedCalls(repo, transport, task, config, kind="judge")
        note, evidence, score = sample()
        envelope = {"stages": {}}
        judge = RagasFaithfulness(calls, config, envelope)
        from reckoner.v1.notes import build_note_context
        from v1_fixtures import decision_fixture

        context = build_note_context(
            decision_fixture(config=config, evidence=evidence), evidence, score, config
        )
        # No blanket fixture approval: prepare the exact next stage and explicitly
        # fabricate a local authorization matching its hash, then replay cached stages.
        for expected_stage in ("judge-statements", "judge-faithfulness"):
            with pytest.raises(api.ApprovalRequired) as pending:
                judge.evaluate(note, context=context)
            assert pending.value.stage == expected_stage
            p = protocol(task, pending.value.request_sha256, provider="anthropic", attempts=1)
            p.update(input_token_ceiling=16000, usd_cap=".1", purpose="judge")
            identified(p, "protocol_id")
            for previous in envelope["stages"].values():
                ProviderBudget(repo._connection).close(previous["protocol_id"])
            authorize(pg.owner_dsn, p)
            envelope["stages"][expected_stage] = p
        assert judge.evaluate(note, context=context) == 1
        assert judge.evaluate(note, context=context) == 1
        assert len(transport.calls) == 2
        assert (
            repo._connection.execute(
                "SELECT count(*) AS n FROM reckoner.v1_provider_calls WHERE purpose='judge'"
            ).fetchone()["n"]
            == 2
        )
        assert (
            repo._connection.execute(
                "SELECT count(*) AS n FROM reckoner.v1_settlements"
            ).fetchone()["n"]
            == 2
        )


def test_late_note_is_evaluated_without_changing_manual_review_snapshot(pg):
    _, lifecycle = modules()
    from reckoner.v1.contracts import validate_v1
    from reckoner.v1.evaluation.notes import evaluate_note_fixtures
    from reckoner.v1.notes import build_note_request
    from reckoner.v1.notes.validate import note_data
    from test_v1_graph_workflow import execute, prepared
    from test_v1_note_eval import Judge
    from v1_fixtures import review_fixture

    repo, task, evidence, settings, _ = prepared(pg)
    with PostgresRepository(pg.runner_dsn) as old:
        legacy = next(
            t for t in old.pending_tasks("baseline-preserved") if t["tenant_id"] == "tenant-a"
        )
    ledger_fixture(pg, legacy)
    with repo:
        decision, _ = execute(repo, task, settings)
        assert lifecycle.note_work(repo, decision)["status"] == "pending"
        review = review_fixture()
        review.update(
            case_id=decision["decision_id"],
            decision_id=decision["decision_id"],
            verdict="decline",
            recommendation=None,
        )
        validate_v1("review", review)
        with V1Repository(pg.owner_dsn) as owner:
            owner._insert(
                "v1_reviews",
                {
                    **{
                        k: review[k]
                        for k in (
                            "tenant_id",
                            "action_id",
                            "case_id",
                            "decision_id",
                            "idempotency_key",
                            "prior_case_version",
                            "reviewer_type",
                            "verdict",
                            "reviewed_at",
                        )
                    },
                    "document": review,
                },
                {"tenant_id": review["tenant_id"], "action_id": review["action_id"]},
            )
            owner._connection.execute(
                "UPDATE reckoner.v1_cases SET status='reviewed',version=2 WHERE "
                "tenant_id=%s AND case_id=%s",
                (task["tenant_id"], decision["decision_id"]),
            )
        score = repo._connection.execute(
            "SELECT score FROM reckoner.v1_provider_responses WHERE call_id=%s",
            (decision["call_id"],),
        ).fetchone()["score"]
        config = repo.workflow_document(
            "v1_configs", {"tenant_id": task["tenant_id"], "config_id": task["config_id"]}
        )
        data = note_data(evidence, score)
        fields = {
            k: data[k]
            for k in ("confidence", "risk_indicators", "entity_neighbourhood", "comparable_cases")
        }
        fields.update(verdict_recommendation="approve", what_would_change_verdict=[])
        p = protocol(
            task,
            content_id(build_note_request(decision, evidence, score, config)),
            provider="anthropic",
            attempts=1,
        )
        p["purpose"] = "online-note"
        identified(p, "protocol_id")
        authorize(pg.owner_dsn, p)
        repo.note_client = Transport([paid_body(json.dumps(fields))])
        repo.note_protocol = p
        execute(repo, task, settings)
        result = lifecycle.note_work(repo, decision)
        assert result["note"] is not None and result["available_before_review"] is False
        assert (
            repo._connection.execute("SELECT count(*) AS n FROM reckoner.v1_notes").fetchone()["n"]
            == 0
        )
        assert (
            repo._connection.execute("SELECT version FROM reckoner.v1_cases").fetchone()["version"]
            == 2
        )

        judge_protocol = protocol(task, provider="anthropic", attempts=1)
        judge_protocol["purpose"] = "judge"
        identified(judge_protocol, "protocol_id")
        verdict, faithfulness = Judge(["approve"]), Judge([1])
        report = evaluate_note_fixtures(
            [
                {
                    "tenant_id": task["tenant_id"],
                    "case_id": decision["decision_id"],
                    "note": result["note"],
                    "evidence": evidence,
                    "score": score,
                    "oracle_verdict": "approve",
                }
            ],
            {"verdict": verdict, "faithfulness": faithfulness},
            1,
            protocol=judge_protocol,
            budget=ProviderBudget(repo._connection),
        )
        assert report["status"] == "passed"
        assert len(verdict.inputs) == len(faithfulness.inputs) == 1
        assert lifecycle.note_work(repo, decision) == result
        assert (
            repo._connection.execute("SELECT count(*) AS n FROM reckoner.v1_notes").fetchone()["n"]
            == 0
        )
        assert (
            repo._connection.execute("SELECT document FROM reckoner.v1_reviews").fetchone()[
                "document"
            ]
            == review
        )


def test_changed_judge_population_rejected_before_dispatch(pg):
    from reckoner.v1.evaluation.notes import evaluate_note_fixtures
    from test_v1_note_eval import Judge, cases

    repo, task, config, request, p = prepared_call(pg)
    p["purpose"] = "judge"
    identified(p, "protocol_id")
    verdict = Judge(["approve"])
    with repo:
        with pytest.raises(ValueError, match="population"):
            evaluate_note_fixtures(
                cases(1),
                {"verdict": verdict, "faithfulness": Judge([1])},
                1,
                protocol=p,
                budget=ProviderBudget(repo._connection),
            )
        assert verdict.inputs == []


def test_repair_cannot_obtain_a_fresh_attempt_allowance(pg):
    api, _ = modules()
    repo, task, config, request, p = prepared_call(pg)
    with repo:
        calls = api.BudgetedCalls(repo, Transport([paid_body()]), task, config, kind="note")
        with pytest.raises(ValueError, match="repair"):
            calls.execute(request, stage="note-repair", protocol=p)


def test_valid_schema_repair_has_two_separate_settlements_in_one_envelope(pg):
    api, _ = modules()
    config, manifest, legacy = setup_run(pg)
    config["limits"]["maximum_attempts"] = 2
    identified(config, "config_id")
    manifest.update(config_id=config["config_id"], run_id="repair-run")
    identified(manifest, "experiment_id")
    with V1Repository(pg.owner_dsn) as owner:
        owner.register_config(config)
        owner.create_run(manifest, config["config_id"])
    ledger_fixture(pg, legacy)
    with V1Repository(pg.runner_dsn) as repo:
        task = repo.task("tenant-a", "repair-run", "task-a")
        request = {
            "model": config["note_model"]["model"],
            "system": "note-v1",
            "messages": [{"role": "user", "content": "simulated"}],
            "temperature": 0,
            "max_tokens": 1000,
        }
        p = protocol(task, content_id(request), provider="anthropic", attempts=2)
        p["purpose"] = "online-note"
        identified(p, "protocol_id")
        authorize(pg.owner_dsn, p)
        transport = Transport([paid_body("bad JSON"), paid_body("{}")])
        calls = api.BudgetedCalls(repo, transport, task, config, kind="note")
        first = calls.execute(request, stage="note-generation", protocol=p)
        second = calls.execute(request, stage="note-repair", protocol=p)
        assert first["call_id"] != second["call_id"]
        assert calls.execute(request, stage="note-repair", protocol=p) == second
        totals = repo._connection.execute(
            "SELECT count(*) AS n,sum(cost) AS cost FROM reckoner.v1_settlements"
        ).fetchone()
        assert totals == {"n": 2, "cost": Decimal(".00014")}
        assert len(transport.calls) == 2


@pytest.mark.parametrize("state", ["pending", "failed", "ready", "evidence", "score", "note"])
def test_durable_eval_verifies_actual_note_and_inputs_before_judging(pg, state):
    from reckoner.v1.evaluation.notes import evaluate_note_fixtures
    from reckoner.v1.notes import CONTENT_FIELDS, build_note_request, validate_note
    from reckoner.v1.notes.lifecycle import note_work
    from reckoner.v1.notes.validate import note_data
    from test_v1_graph_workflow import execute, prepared
    from test_v1_note_eval import Judge

    repo, task, evidence, settings, _ = prepared(pg)
    with repo:
        decision, _ = execute(repo, task, settings)
        score = repo._connection.execute(
            "SELECT score FROM reckoner.v1_provider_responses WHERE call_id=%s",
            (decision["call_id"],),
        ).fetchone()["score"]
        note, _, _ = sample()
        data = note_data(evidence, score)
        note.update(
            tenant_id=task["tenant_id"],
            case_id=decision["decision_id"],
            decision_id=decision["decision_id"],
            evidence_id=evidence["evidence_id"],
        )
        note.update(
            {
                k: data[k]
                for k in (
                    "confidence",
                    "entity_neighbourhood",
                    "risk_indicators",
                    "comparable_cases",
                )
            }
        )
        note["what_would_change_verdict"] = []
        identified(note, "note_id")
        if state != "pending":
            with PostgresRepository(pg.runner_dsn) as old:
                legacy = next(
                    t
                    for t in old.pending_tasks("baseline-preserved")
                    if t["tenant_id"] == "tenant-a"
                )
            ledger_fixture(pg, legacy)
            config = repo.workflow_document(
                "v1_configs", {"tenant_id": task["tenant_id"], "config_id": task["config_id"]}
            )
            req = build_note_request(decision, evidence, score, config)
            approval = protocol(task, content_id(req), provider="anthropic", attempts=1)
            approval["purpose"] = "online-note"
            identified(approval, "protocol_id")
            authorize(pg.owner_dsn, approval)
            response = paid_body(json.dumps({k: note[k] for k in CONTENT_FIELDS}))
            if state == "failed":
                response["finish_reason"] = "refusal"
            repo.note_protocol = approval
            repo.note_client = Transport([response])
            execute(repo, task, settings)
            actual = note_work(repo, decision)
            if state != "failed":
                assert actual["status"] == "succeeded"
                note = actual["note"]
        if state == "evidence":
            evidence["features"]["amount_usd"] = "900.00"
            identified(evidence, "evidence_id")
            note["evidence_id"] = evidence["evidence_id"]
            note["entity_neighbourhood"]["evidence_refs"] = [evidence["evidence_id"]]
        if state == "score":
            score["raw_probability"] = "0.3"
            score["distribution"] = {"fraud": "0.3", "legitimate": "0.7"}
        if state == "note":
            note["verdict_recommendation"] = "decline"
        identified(note, "note_id")
        validate_note(note, evidence, score)
        p = protocol(task, provider="anthropic", attempts=1)
        p["purpose"] = "judge"
        identified(p, "protocol_id")
        cases = [
            {
                "tenant_id": task["tenant_id"],
                "case_id": decision["decision_id"],
                "note": note,
                "evidence": evidence,
                "score": score,
                "oracle_verdict": "approve",
            }
        ]
        budget = ProviderBudget(repo._connection)
        verdict, faithfulness = Judge(["approve"]), Judge([1])
        report = evaluate_note_fixtures(
            cases,
            {"verdict": verdict, "faithfulness": faithfulness},
            1,
            protocol=p,
            budget=budget,
        )
        assert report["status"] == ("passed" if state == "ready" else "failed")
        if state != "ready":
            assert verdict.inputs == [] and faithfulness.inputs == []
            assert report["cases"][0]["schema"] == "fail"
        assert report["quality_acceptance"] == "pending separately approved measured evaluation"
        stored = repo._connection.execute(
            "SELECT document FROM reckoner.v1_note_evaluations"
        ).fetchone()["document"]
        assert stored == report
        if state == "ready":
            assert stored["cases"][0]["judges"]["verdict"]["model"] == "fixture"


def test_invalid_provider_shape_cannot_pin_a_generation_stage(pg):
    api, _ = modules()
    repo, task, config, request, p = prepared_call(pg)
    request["messages"][0]["content"] = {"invalid": "nested"}
    p["tasks"][0]["request_sha256"] = content_id(request)
    identified(p, "protocol_id")
    transport = Transport([paid_body()])
    with repo:
        with pytest.raises(ValueError):
            api.BudgetedCalls(repo, transport, task, config, kind="note").execute(
                request, stage="note-generation", protocol=p
            )
        assert not transport.calls
        assert (
            repo._connection.execute(
                "SELECT count(*) AS n FROM reckoner.v1_generation_stages"
            ).fetchone()["n"]
            == 0
        )


def test_calibrated_note_and_eval_share_persisted_routing_without_rewriting_score(pg):
    from copy import deepcopy

    import httpx
    from reckoner.v1.evaluation.notes import evaluate_note_fixtures
    from reckoner.v1.notes import build_note_context, build_note_request
    from reckoner.v1.notes.lifecycle import note_work
    from reckoner.v1.providers.jev import JevClient
    from test_v1_graph_workflow import calibrated_setup, execute
    from test_v1_jev import response
    from test_v1_note_eval import Judge

    repo, task, settings, _ = calibrated_setup(pg)
    scorer_body = response()
    scorer_body["answers"]["risk"]["probabilities"] = {"fraud": 0.4, "legitimate": 0.6}
    repo.scorer_client = JevClient(
        "fabricated-only",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=scorer_body)),
    )
    with PostgresRepository(pg.runner_dsn) as old:
        legacy = next(
            t for t in old.pending_tasks("baseline-preserved") if t["tenant_id"] == "tenant-a"
        )
    ledger_fixture(pg, legacy)
    with repo:
        decision, _ = execute(repo, task, settings)
        assert decision["outcome"] == "escalate"
        assert decision["raw_probability"] != decision["effective_probability"]
        evidence = repo.workflow_document(
            "v1_evidence", {"tenant_id": task["tenant_id"], "evidence_id": decision["evidence_id"]}
        )
        config = repo.workflow_document(
            "v1_configs", {"tenant_id": task["tenant_id"], "config_id": task["config_id"]}
        )
        score = repo._connection.execute(
            "SELECT score FROM reckoner.v1_provider_responses WHERE call_id=%s",
            (decision["call_id"],),
        ).fetchone()["score"]
        original = deepcopy(score)
        context = build_note_context(decision, evidence, score, config)
        fields = {
            k: context[k]
            for k in ("confidence", "risk_indicators", "entity_neighbourhood", "comparable_cases")
        }
        fields.update(verdict_recommendation="approve", what_would_change_verdict=[])
        request = build_note_request(decision, evidence, score, config)
        approval = protocol(task, content_id(request), provider="anthropic", attempts=1)
        approval["purpose"] = "online-note"
        identified(approval, "protocol_id")
        authorize(pg.owner_dsn, approval)
        repo.note_protocol = approval
        repo.note_client = Transport([paid_body(json.dumps(fields))])
        execute(repo, task, settings)
        actual = note_work(repo, decision)
        assert actual["status"] == "succeeded"
        assert json.loads(repo.note_client.calls[0]["messages"][0]["content"]) == context
        assert context["routing"] == {
            "raw_probability": "0.4",
            "effective_probability": decision["effective_probability"],
            "score_mode": "calibrated",
            "calibration_id": settings["calibration"]["calibration_id"],
        }
        assert context["confidence"]["value"] == "0.8"
        assert "oracle" not in json.dumps(context)
        judge_protocol = protocol(task, provider="anthropic", attempts=1)
        judge_protocol["purpose"] = "judge"
        identified(judge_protocol, "protocol_id")
        faithfulness = Judge([1])
        report = evaluate_note_fixtures(
            [
                {
                    "tenant_id": task["tenant_id"],
                    "case_id": decision["decision_id"],
                    "note": actual["note"],
                    "evidence": evidence,
                    "score": score,
                    "oracle_verdict": "approve",
                }
            ],
            {"verdict": Judge(["approve"]), "faithfulness": faithfulness},
            1,
            protocol=judge_protocol,
            budget=ProviderBudget(repo._connection),
        )
        assert report["status"] == "passed"
        assert faithfulness.inputs[0][1]["context"] == context
        persisted = repo._connection.execute(
            "SELECT score FROM reckoner.v1_provider_responses WHERE call_id=%s",
            (decision["call_id"],),
        ).fetchone()["score"]
        assert persisted == original
        assert persisted["adjusted_probability"] is None and persisted["calibration_id"] is None

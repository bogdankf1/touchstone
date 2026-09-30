"""Disposable PostgreSQL accounting and fabricated-provider crash/retry tests."""

import importlib
from copy import deepcopy
from decimal import Decimal
from threading import Barrier, Thread

import httpx
import psycopg
import pytest
from reckoner.contracts import content_id
from reckoner.v1.storage.repository import V1Repository
from test_v1_jev import response
from test_v1_storage import setup_run
from v1_fixtures import evidence_fixture, identified

pytestmark = pytest.mark.integration


def modules():
    try:
        return (
            importlib.import_module("reckoner.v1.storage.budget"),
            importlib.import_module("reckoner.v1.storage.attempts"),
            importlib.import_module("reckoner.v1.providers.jev"),
        )
    except ModuleNotFoundError:
        pytest.fail("v1 provider accounting is not implemented")


def protocol(task, request_hash="a" * 64, cap="0.01", provider="typesafe", attempts=3):
    return identified(
        {
            "tenant_id": task["tenant_id"],
            "run_id": task["run_id"],
            "provider": provider,
            "purpose": "fabricated",
            "model": "jev-1.13.0"
            if provider == "typesafe"
            else "anthropic/claude-haiku-4-5-20251001",
            "input_token_ceiling": 2000,
            "max_output_tokens": 1000,
            "maximum_attempts": attempts,
            "usd_cap": cap,
            "approved": True,
            "tasks": [
                {
                    "task_id": task["task_id"],
                    "transaction_id": task["transaction_id"],
                    "request_sha256": request_hash,
                }
            ],
        },
        "protocol_id",
    )


def call(task, protocol, call_id="call-a"):
    return {
        **{
            k: protocol[k]
            for k in (
                "tenant_id",
                "run_id",
                "provider",
                "purpose",
                "model",
                "input_token_ceiling",
                "max_output_tokens",
            )
        },
        "task_id": task["task_id"],
        "transaction_id": task["transaction_id"],
        "call_id": call_id,
        "request_sha256": protocol["tasks"][0]["request_sha256"],
    }


def test_last_cent_concurrency_and_protocol_allocations_are_counted_once(pg):
    budget, _, _ = modules()
    _, manifest, _ = setup_run(pg)
    task = {
        "tenant_id": "tenant-a",
        "run_id": manifest["run_id"],
        "task_id": "task-a",
        "transaction_id": manifest["tasks"][0]["transaction_id"],
    }
    first = protocol(task, cap="9.99")
    with V1Repository(pg.runner_dsn) as repo:
        ledger = budget.ProviderBudget(repo._connection)
        ledger.reserve(call(task, first), Decimal("0.01"), first)
        assert ledger.remaining("typesafe") == Decimal("0.01")
    outcomes, errors = [], []
    barrier = Barrier(2)

    def reserve(number):
        try:
            with V1Repository(pg.runner_dsn) as repo:
                p = protocol(task, request_hash=str(number) * 64)
                barrier.wait()
                try:
                    budget.ProviderBudget(repo._connection).reserve(
                        call(task, p, f"call-{number}"), Decimal(".01"), p
                    )
                except budget.BudgetExceeded:
                    outcomes.append("blocked")
                else:
                    outcomes.append("reserved")
        except Exception as error:
            errors.append(error)

    threads = [Thread(target=reserve, args=(n,)) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert sorted(outcomes) == ["blocked", "reserved"]


def test_uncertain_settlement_and_idempotency_overage_and_close(pg):
    budget, _, _ = modules()
    _, manifest, _ = setup_run(pg)
    task = {"tenant_id": "tenant-a", "run_id": manifest["run_id"], **manifest["tasks"][0]}
    p = protocol(task, cap="1")
    with V1Repository(pg.runner_dsn) as repo:
        ledger = budget.ProviderBudget(repo._connection)
        reservation = ledger.reserve(call(task, p), Decimal(".01"), p)
        assert ledger.reserve(call(task, p), Decimal(".01"), p) == reservation
        ledger.settle("call-a", None, None)
        ledger.settle("call-a", None, None)
        ledger.close(p["protocol_id"])
        assert ledger.remaining("typesafe") == Decimal("9.99")
        ledger.settle("call-a", {"input_tokens": 1, "output_tokens": 0}, Decimal(".011"))
        assert ledger.remaining("typesafe") == Decimal("9.989")
        with pytest.raises(ValueError, match="conflict"):
            ledger.settle("call-a", {"input_tokens": 1, "output_tokens": 0}, Decimal(".01"))
        with pytest.raises(budget.BudgetExceeded, match="overage"):
            ledger.reserve(call(task, p, "next"), Decimal(".001"), p)


def test_anthropic_requires_verified_legacy_provenance_and_counts_it_once(pg):
    from reckoner.storage.budget import BudgetLedger
    from reckoner.storage.postgres import PostgresRepository

    budget, _, _ = modules()
    _, manifest, legacy = setup_run(pg)
    task = {"tenant_id": "tenant-a", "run_id": manifest["run_id"], **manifest["tasks"][0]}
    p = protocol(task, cap="1", provider="anthropic")
    with V1Repository(pg.runner_dsn) as repo:
        with pytest.raises(budget.BudgetExceeded, match="legacy"):
            budget.ProviderBudget(repo._connection).reserve(call(task, p), Decimal(".1"), p)
    # Explicitly fabricated 1,040-row legacy fixture with the original aggregate.
    with PostgresRepository(pg.runner_dsn) as repo:
        first = BudgetLedger(repo).reserve(legacy, Decimal(".493151"))
        BudgetLedger(repo).settle(
            first["call_id"], Decimal(".493151"), {"input_tokens": 1, "output_tokens": 0}
        )
    with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
        owner.execute(
            "INSERT INTO reckoner.attempts "
            "(tenant_id,run_id,task_id,call_id,status,maximum_cost,actual_cost) "
            "SELECT tenant_id,run_id,task_id,'fabricated-legacy-' || n,'responded',0,0 "
            "FROM reckoner.attempts CROSS JOIN generate_series(1,1039) n WHERE call_id=%s",
            (first["call_id"],),
        )
        owner.execute(
            "INSERT INTO reckoner.budget_entries "
            "(tenant_id,run_id,task_id,call_id,purpose,status,maximum_cost,actual_cost) "
            "SELECT tenant_id,run_id,task_id,call_id,'baseline','settled',0,0 "
            "FROM reckoner.attempts WHERE call_id LIKE 'fabricated-legacy-%'"
        )
        budget.ProviderBudget(owner).verify_legacy()
    with V1Repository(pg.runner_dsn) as repo:
        ledger = budget.ProviderBudget(repo._connection)
        ledger.reserve(call(task, p), Decimal(".1"), p)
        assert ledger.remaining("anthropic") == Decimal("8.506849")
        assert ledger.remaining("typesafe") == Decimal("10")


def scoring(pg, attempts=3):
    _, attempts_module, jev = modules()
    config, manifest, _ = setup_run(pg)
    config["limits"]["maximum_attempts"] = attempts
    identified(config, "config_id")
    manifest["config_id"] = config["config_id"]
    manifest["run_id"] = "score-run"
    identified(manifest, "experiment_id")
    with V1Repository(pg.owner_dsn) as owner:
        owner.register_config(config)
        owner.create_run(manifest, config["config_id"])
    repo = V1Repository(pg.runner_dsn)
    task = repo.task("tenant-a", manifest["run_id"], "task-a")
    evidence = evidence_fixture(transaction_id=task["transaction_id"])
    evidence["query_time"] = task["transaction"]["occurred_at"]
    evidence["cutoffs"]["history_before"] = evidence["query_time"]
    evidence["cutoffs"]["resolved_before"] = evidence["query_time"]
    identified(evidence, "evidence_id")
    request = jev.build_request(task["transaction"], evidence)
    p = protocol(task, content_id(request), attempts=attempts)
    return repo, task, evidence, p, attempts_module, jev


def test_score_cost_response_persisted_restart_does_not_send_again(pg):
    repo, task, evidence, p, attempts, jev = scoring(pg)
    count = []

    def handle(_):
        count.append(1)
        return httpx.Response(200, json=response())

    client = jev.JevClient("fabricated-only", transport=httpx.MockTransport(handle))
    with repo:
        score = attempts.score_task(repo, client, task, evidence, p)
        assert score["cost"]["amount"] == "0.000084"
        assert attempts.score_task(repo, client, task, evidence, p) == score
    assert len(count) == 1
    with V1Repository(pg.runner_dsn) as restarted:
        assert attempts.score_task(restarted, client, task, evidence, p) == score
    assert len(count) == 1


@pytest.mark.parametrize("status,want", [(401, 1), (422, 1), (429, 3), (529, 3)])
def test_retry_bounds_and_separate_liability(pg, monkeypatch, status, want):
    repo, task, evidence, p, attempts, jev = scoring(pg)
    monkeypatch.setattr(attempts.time, "sleep", lambda _: None)
    count = []

    def handle(_):
        count.append(1)
        return httpx.Response(status, text="private error")

    client = jev.JevClient("fabricated-only", transport=httpx.MockTransport(handle))
    with repo:
        result = attempts.score_task(repo, client, task, evidence, p)
        assert result["attempt_status"] == "failed"
        rows = repo._connection.execute("SELECT * FROM reckoner.v1_provider_calls").fetchall()
        assert len(rows) == want
        assert len({r["call_id"] for r in rows}) == want
    assert len(count) == want


def test_ambiguous_timeout_retains_liability_and_never_retries_on_restart(pg):
    repo, task, evidence, p, attempts, jev = scoring(pg)
    count = []

    def handle(_):
        count.append(1)
        raise httpx.ReadTimeout("fabricated")

    client = jev.JevClient("fabricated-only", transport=httpx.MockTransport(handle))
    with repo:
        result = attempts.score_task(repo, client, task, evidence, p)
        assert result["attempt_status"] == "uncertain"
        assert attempts.score_task(repo, client, task, evidence, p) == result
        assert (
            repo._connection.execute("SELECT cost FROM reckoner.v1_settlements").fetchone()["cost"]
            is None
        )
    assert len(count) == 1


def test_protocol_request_hash_model_and_bounds_checked_before_http(pg):
    repo, task, evidence, p, attempts, jev = scoring(pg)
    client = jev.JevClient(
        "fabricated-only", transport=httpx.MockTransport(lambda _: pytest.fail("must not dispatch"))
    )
    with repo:
        for change in (
            {"model": "jev-latest"},
            {"input_token_ceiling": 1},
            {"maximum_attempts": 4},
            {"approved": False},
        ):
            invalid = identified({**p, **change}, "protocol_id")
            with pytest.raises(ValueError):
                attempts.score_task(repo, client, task, evidence, invalid)


def test_unknown_pricing_and_nested_oracle_evidence_prevent_dispatch(pg):
    repo, task, evidence, p, attempts, jev = scoring(pg)
    client = jev.JevClient(
        "fabricated-only", transport=httpx.MockTransport(lambda _: pytest.fail("must not dispatch"))
    )
    invalid = deepcopy(evidence)
    invalid["features"]["oracle"] = {"fraud": True}
    identified(invalid, "evidence_id")
    with repo:
        with pytest.raises(ValueError):
            attempts.score_task(repo, client, task, invalid, p)
        with V1Repository(pg.owner_dsn) as owner:
            config = attempts._configuration(owner, task)
            config["scorer"]["price_table"]["input_per_million"] = "0.043"
            identified(config["scorer"]["price_table"], "price_table_version")
            identified(config, "config_id")
            owner.register_config(config)
        with V1Repository(pg.owner_dsn) as owner:
            run = owner._connection.execute(
                "SELECT document FROM reckoner.v1_runs WHERE tenant_id=%s AND run_id=%s",
                (task["tenant_id"], task["run_id"]),
            ).fetchone()["document"]
            run["run_id"] = "unknown-price-run"
            run["config_id"] = config["config_id"]
            identified(run, "experiment_id")
            owner.create_run(run, config["config_id"])
        task = repo.task(task["tenant_id"], run["run_id"], task["task_id"])
        p = protocol(task, content_id(jev.build_request(task["transaction"], evidence)))
        with pytest.raises(ValueError, match="pricing"):
            attempts.score_task(repo, client, task, evidence, p)


def test_crash_after_reservation_before_response_is_uncertain_without_http(pg):
    repo, task, evidence, p, attempts, jev = scoring(pg)
    budget, _, _ = modules()
    config, request, digest = attempts._preflight(repo, task, evidence, p)
    reservation = {**call(task, p, "lost-response"), "request_document": request}
    with repo:
        budget.ProviderBudget(repo._connection).reserve(reservation, Decimal(".000084"), p)
        result = attempts.score_task(
            repo,
            jev.JevClient(
                "fabricated-only",
                transport=httpx.MockTransport(lambda _: pytest.fail("duplicate dispatch")),
            ),
            task,
            evidence,
            p,
        )
        assert result["attempt_status"] == "uncertain"
        assert result["cost"]["amount"] is None


def test_provider_response_without_usage_retains_maximum(pg):
    repo, task, evidence, p, attempts, jev = scoring(pg)
    body = response()
    body.pop("usage")
    with repo:
        result = attempts.score_task(
            repo,
            jev.JevClient(
                "fabricated-only",
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
            ),
            task,
            evidence,
            p,
        )
        assert result["attempt_status"] == "responded"
        assert result["cost"]["status"] == "uncertain"
        budget, _, _ = modules()
        ledger = budget.ProviderBudget(repo._connection)
        ledger.close(p["protocol_id"])
        assert ledger.remaining("typesafe") == Decimal("9.999916")


def test_retry_after_over_60_defers_without_second_http(pg, monkeypatch):
    repo, task, evidence, p, attempts, jev = scoring(pg)
    monkeypatch.setattr(attempts.time, "sleep", lambda _: pytest.fail("must defer long hint"))
    count = []

    def handle(_):
        count.append(1)
        return httpx.Response(429, text="fabricated", headers={"Retry-After": "61"})

    with repo:
        client = jev.JevClient("fabricated-only", transport=httpx.MockTransport(handle))
        result = attempts.score_task(repo, client, task, evidence, p)
        assert result["attempt_status"] == "failed"
        assert (
            attempts.score_task(repo, client, task, evidence, p)["scorer_status"] == "unavailable"
        )
    assert len(count) == 1


def test_provider_five_transient_failures_open_circuit_and_successful_probe_closes(pg, monkeypatch):
    repo, task, evidence, p, attempts, jev = scoring(pg)
    monkeypatch.setattr(attempts.time, "sleep", lambda _: None)
    calls = []

    def handle(_):
        calls.append(1)
        return httpx.Response(529, text="fabricated")

    with repo:
        client = jev.JevClient("fabricated-only", transport=httpx.MockTransport(handle))
        attempts.score_task(repo, client, task, evidence, p)
        with V1Repository(pg.owner_dsn) as owner:
            run = owner._connection.execute(
                "SELECT document FROM reckoner.v1_runs WHERE tenant_id=%s AND run_id=%s",
                (task["tenant_id"], task["run_id"]),
            ).fetchone()["document"]
            run["run_id"] = "second-score-run"
            identified(run, "experiment_id")
            owner.create_run(run, run["config_id"])
        other = repo.task(task["tenant_id"], run["run_id"], task["task_id"])
        second = protocol(other, content_id(jev.build_request(other["transaction"], evidence)))
        skipped = attempts.score_task(repo, client, other, evidence, second)
        assert skipped["scorer_status"] == "unavailable"
        assert len(calls) == 5
        state = repo._connection.execute("SELECT * FROM reckoner.v1_provider_state").fetchone()
        assert state["consecutive_failures"] == 5
        assert state["open_until"] is not None
        with psycopg.connect(pg.owner_dsn) as owner:
            owner.execute(
                "UPDATE reckoner.v1_provider_state SET open_until=now()-interval '1 second'"
            )
        succeeded = attempts.score_task(
            repo,
            jev.JevClient(
                "fabricated-only",
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response())),
            ),
            other,
            evidence,
            second,
        )
        assert succeeded["attempt_status"] == "responded"
        state = repo._connection.execute("SELECT * FROM reckoner.v1_provider_state").fetchone()
        assert state["open_until"] is None and state["consecutive_failures"] == 0


def test_cli_score_checks_exact_cases_before_loading_environment(tmp_path):
    import json
    from argparse import Namespace

    from reckoner.v1.cli import execute
    from v1_fixtures import config_fixture, experiment_fixture

    manifest = experiment_fixture(config_fixture())
    p = protocol(
        {"tenant_id": manifest["tenant_id"], "run_id": manifest["run_id"], **manifest["tasks"][0]}
    )
    p["tasks"][0]["transaction_id"] = "unapproved-case"
    identified(p, "protocol_id")
    manifest_path, protocol_path = tmp_path / "manifest.json", tmp_path / "protocol.json"
    manifest_path.write_text(json.dumps(manifest))
    protocol_path.write_text(json.dumps(p))
    with pytest.raises(ValueError, match="cases"):
        execute(
            Namespace(
                v1_command="score",
                manifest=manifest_path,
                protocol=protocol_path,
                env_file=tmp_path / "unread-secret.env",
            )
        )


def test_nan_http_body_is_protected_failure_with_uncertain_cost(pg):
    repo, task, evidence, p, attempts, jev = scoring(pg)
    text = (
        '{"model":"jev-1.13.0","answers":{"risk":{"type":"choice",'
        '"choice":"legitimate","confidence":0.8,"probabilities":{"fraud":NaN,'
        '"legitimate":0.8}}}}'
    )
    with repo:
        result = attempts.score_task(
            repo,
            jev.JevClient(
                "fabricated-only",
                transport=httpx.MockTransport(lambda _: httpx.Response(200, text=text)),
            ),
            task,
            evidence,
            p,
        )
        assert result["attempt_status"] == "failed"
        assert result["cost"]["status"] == "uncertain"
        assert (
            repo._connection.execute("SELECT body FROM reckoner.v1_provider_responses").fetchone()[
                "body"
            ]
            == text
        )


def test_expired_circuit_allows_one_probe_and_rate_deadline_is_persisted(pg):
    repo, task, evidence, p, attempts, jev = scoring(pg)
    from reckoner.v1.storage.budget import ProviderBudget

    with repo:
        repo._connection.execute(
            "INSERT INTO reckoner.v1_provider_state "
            "(provider,consecutive_failures,open_until) "
            "VALUES ('typesafe',5,now()-interval '1 second')"
        )
        barrier, outcomes, errors = Barrier(2), [], []

        def claim_probe(number):
            try:
                with V1Repository(pg.runner_dsn) as worker:
                    barrier.wait()
                    outcomes.append(
                        attempts._claim(
                            worker,
                            ProviderBudget(worker._connection),
                            call(task, p, f"probe-{number}"),
                            Decimal(".000084"),
                            p,
                        )
                    )
            except Exception as error:
                errors.append(error)

        threads = [Thread(target=claim_probe, args=(n,)) for n in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        assert errors == []
        assert sum(skip is None for skip, _ in outcomes) == 1
        assert (
            repo._connection.execute(
                "SELECT count(*) AS n FROM reckoner.v1_provider_calls"
            ).fetchone()["n"]
            == 1
        )
        repo._connection.execute(
            "UPDATE reckoner.v1_provider_state SET active_call=NULL,"
            "next_dispatch_at=now()+interval '0.5 seconds'"
        )
        skip, wait = attempts._claim(
            repo,
            ProviderBudget(repo._connection),
            call(task, p, "rate-probe"),
            Decimal(".000084"),
            p,
        )
        assert skip is None and 0 < wait <= 0.5

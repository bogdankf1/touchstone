"""Actual checkpoint failure, uncertain dispatch, task races and opaque identities."""

import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from threading import Event

import httpx
import psycopg
import pytest
from reckoner.v1.storage.repository import V1Repository
from test_v1_budget import call
from test_v1_graph_workflow import execute, prepared, workflow
from test_v1_jev import response
from v1_fixtures import identified, reauthorize

pytestmark = pytest.mark.integration


def test_saved_response_and_settlement_survive_actual_checkpoint_failure(pg):
    api, checkpoints = workflow()
    repo, task, _, settings, calls = prepared(pg)

    class CrashAfterScore(checkpoints.PostgresCheckpointer):
        def put_writes(self, config, writes, task_id, task_path=""):
            if any(channel == "scorer_status" for channel, _ in writes):
                raise RuntimeError("fabricated checkpoint crash")
            return super().put_writes(config, writes, task_id, task_path)

        def put(self, config, checkpoint, metadata, new_versions):
            if checkpoint["channel_values"].get("scorer_status"):
                raise RuntimeError("fabricated checkpoint crash")
            return super().put(config, checkpoint, metadata, new_versions)

    client = repo.scorer_client
    with repo:
        with pytest.raises(RuntimeError, match="checkpoint crash"):
            execute(repo, task, settings, CrashAfterScore)
    assert len(calls) == 1
    with psycopg.connect(pg.owner_dsn) as owner:
        assert (
            owner.execute("SELECT count(*) FROM reckoner.v1_provider_responses").fetchone()[0] == 1
        )
        assert owner.execute("SELECT count(*) FROM reckoner.v1_decisions").fetchone()[0] == 0
    # A later settings version must not alter the already-started run's threshold.
    with V1Repository(pg.owner_dsn) as owner:
        changed_config = owner.workflow_document(
            "v1_configs", {"tenant_id": task["tenant_id"], "config_id": task["config_id"]}
        )
        changed_threshold = owner.workflow_document(
            "threshold_configs",
            {"tenant_id": task["tenant_id"], "config_id": changed_config["threshold_config_id"]},
        )
        changed_threshold["parameters"]["t_high"] = "0.10"
        identified(changed_threshold, "config_id")
        from reckoner.storage.postgres import PostgresRepository

        with PostgresRepository(pg.owner_dsn) as baseline:
            baseline.register_threshold_config(changed_threshold)
        changed_config["threshold_config_id"] = changed_threshold["config_id"]
        identified(changed_config, "config_id")
        owner.register_config(changed_config)
    with V1Repository(pg.runner_dsn, scorer_client=client) as restarted:
        with checkpoints.PostgresCheckpointer(pg.runner_dsn, task["tenant_id"]) as saver:
            graph = api.build_graph(saver)
            before = graph.get_state(checkpoints.task_config(task))
            assert before.next == ("score",)
            result = api.resume_task(
                restarted, graph, task["tenant_id"], task["run_id"], task["task_id"]
            )
            assert result["scorer_status"] == "succeeded"
            assert result["outcome"] == "escalate"
            assert result["config_id"] == task["config_id"]
            assert Decimal(result["effective_high_threshold"]) == Decimal(".90")
            assert (
                api.resume_task(
                    restarted, graph, task["tenant_id"], task["run_id"], task["task_id"]
                )
                == result
            )
            assert len(calls) == 1
            from reckoner.v1.storage.budget import ProviderBudget

            assert ProviderBudget(restarted._connection).remaining("typesafe") == Decimal("9.99")
    with psycopg.connect(pg.owner_dsn) as owner:
        assert owner.execute("SELECT count(*) FROM reckoner.v1_provider_calls").fetchone()[0] == 1
        assert owner.execute("SELECT count(*) FROM reckoner.v1_settlements").fetchone()[0] == 1


def test_ambiguous_reserved_call_stays_uncertain_without_http(pg):
    repo, task, _, settings, calls = prepared(pg)
    from reckoner.v1.storage.budget import ProviderBudget

    with repo:
        ProviderBudget(repo._connection).reserve(
            call(task, settings["protocol"]), Decimal(".000084"), settings["protocol"]
        )
        decision, state = execute(repo, task, settings)
        assert decision["outcome"] == "escalate"
        assert decision["scorer_status"] == "uncertain"
        assert state.values["cost_status"] == "uncertain"
        assert decision["call_id"] == "call-a"
        assert calls == []


def test_budget_exhaustion_keeps_task_incomplete_and_no_decision(pg):
    repo, task, _, settings, calls = prepared(pg)
    from reckoner.v1.storage.budget import BudgetExceeded

    previous = settings["protocol"]["protocol_id"]
    settings["protocol"]["usd_cap"] = "0.000001"
    identified(settings["protocol"], "protocol_id")
    reauthorize(pg.owner_dsn, previous, settings["protocol"])
    with repo:
        with pytest.raises(BudgetExceeded):
            execute(repo, task, settings)
        assert (
            repo.task(task["tenant_id"], task["run_id"], task["task_id"])["status"] != "completed"
        )
        assert (
            repo.workflow_document(
                "v1_decisions", {k: task[k] for k in ("tenant_id", "run_id", "task_id")}
            )
            is None
        )
        assert calls == []


def test_same_task_concurrency_waits_for_first_owner_and_returns_same_decision(pg):
    api, checkpoints = workflow()
    repo, task, _, settings, calls = prepared(pg)
    from reckoner.v1.providers.jev import JevClient

    entered, release, second_started = Event(), Event(), Event()

    def handle(request):
        calls.append(request)
        entered.set()
        assert release.wait(10)
        return httpx.Response(200, json=response())

    client = JevClient("fabricated-only", transport=httpx.MockTransport(handle))
    repo.__exit__(None, None, None)

    def worker(second=False):
        with V1Repository(pg.runner_dsn, scorer_client=client) as connection:
            with checkpoints.PostgresCheckpointer(pg.runner_dsn, task["tenant_id"]) as saver:
                if second:
                    second_started.set()
                return api.run_task(connection, api.build_graph(saver), task, deepcopy(settings))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker)
        assert entered.wait(10)
        second = pool.submit(worker, True)
        assert second_started.wait(10)
        # Require an actual second-session lock wait while the first is inside HTTP.
        try:
            with psycopg.connect(pg.owner_dsn, autocommit=True) as owner:
                deadline = time.monotonic() + 5
                while not owner.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                    "AND NOT granted AND database=(SELECT oid FROM pg_database "
                    "WHERE datname=current_database()))"
                ).fetchone()[0]:
                    assert time.monotonic() < deadline, "second worker never waited on task lock"
                    time.sleep(0.01)
                assert not second.done()
                assert (
                    owner.execute("SELECT count(*) FROM reckoner.v1_decisions").fetchone()[0] == 0
                )
        finally:
            release.set()
        assert first.result(15) == second.result(15)
    assert len(calls) == 1


def test_opaque_ids_and_cross_task_policy_conflict(pg):
    repo, original, evidence, settings, calls = prepared(pg)
    from reckoner.contracts import content_id
    from reckoner.v1.providers.jev import build_request
    from v1_fixtures import experiment_fixture

    config = repo.workflow_document(
        "v1_configs", {"tenant_id": original["tenant_id"], "config_id": original["config_id"]}
    )
    manifest = experiment_fixture(config, original["transaction_id"])
    manifest["run_id"] = "opaque/run/<run>"
    manifest["tasks"][0]["task_id"] = "task/one/<x>"
    with psycopg.connect(pg.owner_dsn) as owner:
        other = owner.execute(
            "SELECT transaction_id FROM reckoner.transactions WHERE tenant_id='tenant-a' "
            "AND transaction_id<>%s LIMIT 1",
            (original["transaction_id"],),
        ).fetchone()[0]
    manifest["tasks"].append({"task_id": "task/two", "transaction_id": other})
    identified(manifest, "experiment_id")
    with V1Repository(pg.owner_dsn) as owner:
        owner.create_run(manifest, config["config_id"])
    with repo:
        task = repo.task("tenant-a", manifest["run_id"], manifest["tasks"][0]["task_id"])
        p = settings["protocol"]
        previous = p["protocol_id"]
        p["run_id"] = task["run_id"]
        p["tasks"] = [
            {
                "task_id": task["task_id"],
                "transaction_id": task["transaction_id"],
                "request_sha256": content_id(build_request(task["transaction"], evidence)),
            }
        ]
        identified(p, "protocol_id")
        reauthorize(pg.owner_dsn, previous, p)
        decision, _ = execute(repo, task, settings)
        assert decision["run_id"] == manifest["run_id"]
        assert decision["task_id"] == "task/one/<x>"
        second_task = repo.task("tenant-a", manifest["run_id"], "task/two")
        from v1_fixtures import evidence_fixture

        second_evidence = evidence_fixture(transaction_id=other)
        second_evidence["query_time"] = second_task["transaction"]["occurred_at"]
        second_evidence["cutoffs"] = {
            "history_before": second_evidence["query_time"],
            "resolved_before": second_evidence["query_time"],
            "graph_before": None,
        }
        identified(second_evidence, "evidence_id")
        repo.persist_evidence(second_evidence)
        with pytest.raises(ValueError, match="conflict"):
            execute(
                repo,
                second_task,
                {
                    **settings,
                    "evidence_id": second_evidence["evidence_id"],
                    "evidence_mode": "gds-augmented",
                },
            )
        assert len(calls) == 1


def test_checkpoint_identity_payload_and_pinned_references_cannot_be_changed(pg):
    _, checkpoints = workflow()
    repo, task, _, settings, _ = prepared(pg)
    with repo:
        execute(repo, task, settings)
    with checkpoints.PostgresCheckpointer(pg.runner_dsn, task["tenant_id"]) as saver:
        config = checkpoints.task_config(task)
        checkpoint = saver.get_tuple(config)
        for field, value in (
            ("tenant_id", "tenant-b"),
            ("evidence_id", "f" * 64),
            ("config_id", "e" * 64),
            ("calibration_id", "d" * 64),
            ("protocol_id", "c" * 64),
        ):
            altered = deepcopy(checkpoint.checkpoint)
            altered["id"] = altered["id"] + field
            altered["channel_values"][field] = value
            with pytest.raises(ValueError, match="pinned|identity"):
                saver.put(config, altered, checkpoint.metadata, {})


def test_failure_after_decision_commit_returns_same_decision_on_resume(pg):
    api, checkpoints = workflow()
    repo, task, _, settings, calls = prepared(pg)

    class CrashAfterDecision(checkpoints.PostgresCheckpointer):
        def put_writes(self, config, writes, task_id, task_path=""):
            if any(channel == "decision_id" for channel, _ in writes):
                raise RuntimeError("fabricated terminal checkpoint crash")
            return super().put_writes(config, writes, task_id, task_path)

        def put(self, config, checkpoint, metadata, new_versions):
            if checkpoint["channel_values"].get("decision_id"):
                raise RuntimeError("fabricated terminal checkpoint crash")
            return super().put(config, checkpoint, metadata, new_versions)

    with repo:
        with pytest.raises(RuntimeError, match="checkpoint crash"):
            execute(repo, task, settings, CrashAfterDecision)
        before = repo.workflow_document(
            "v1_decisions", {k: task[k] for k in ("tenant_id", "run_id", "task_id")}
        )
        assert before is not None
        with checkpoints.PostgresCheckpointer(pg.runner_dsn, task["tenant_id"]) as saver:
            graph = api.build_graph(saver)
            assert (
                api.resume_task(repo, graph, task["tenant_id"], task["run_id"], task["task_id"])
                == before
            )
            assert not graph.get_state(checkpoints.task_config(task)).next
        assert len(calls) == 1

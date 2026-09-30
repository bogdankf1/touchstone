"""Real disposable Postgres outbox atomicity and exact immutable OTLP delivery."""

import importlib
from copy import deepcopy

import pytest
from test_v1_storage import repository, setup_run
from v1_fixtures import decision_fixture, evidence_fixture

pytestmark = pytest.mark.integration


def outbox():
    try:
        return importlib.import_module("reckoner.v1.telemetry.outbox")
    except ModuleNotFoundError:
        pytest.fail("v1 OTLP outbox missing")


def test_run_declaration_and_decision_event_are_atomic_and_idempotent(pg):
    config, manifest, task = setup_run(pg)
    api = outbox()
    with repository(pg.runner_dsn) as repo:
        with pytest.raises(ValueError, match="mode"):
            api.collect_scoring_run(repo, task["tenant_id"], task["run_id"])
        evidence = evidence_fixture(transaction_id=task["transaction"]["transaction_id"])
        repo.persist_evidence(evidence)
        decision = decision_fixture(config, evidence, degraded=True)
        with pytest.raises(RuntimeError):
            with repo._connection.transaction():
                repo.persist_decision(decision)
                raise RuntimeError("transaction interrupted")
        assert (
            repo.workflow_document(
                "v1_decisions", {k: decision[k] for k in ("tenant_id", "run_id", "task_id")}
            )
            is None
        )
        repo.persist_decision(decision)
        rows = repo._connection.execute(
            "SELECT document,payload FROM reckoner.v1_otlp_delivery ORDER BY event_id"
        ).fetchall()
        assert len(rows) == 3
        assert {r["document"]["schema_version"] for r in rows} == {
            "run-declaration-v1",
            "measurement-v1",
        }
        repo.persist_decision(decision)
        assert api.enqueue_events(repo, [r["document"] for r in rows]) == 0
        changed = deepcopy(
            next(r["document"] for r in rows if r["document"]["schema_version"] == "measurement-v1")
        )
        changed["node_name"] = "conflicting"
        with pytest.raises(ValueError, match="conflict"):
            api.enqueue_events(repo, [changed])
        assert (
            repo._connection.execute(
                "SELECT count(*) AS n FROM reckoner.v1_otlp_delivery"
            ).fetchone()["n"]
            == 3
        )


def test_export_failure_and_partial_rejection_never_mark_delivered(pg):
    setup_run(pg)
    api = outbox()

    class Receiver:
        def __init__(self):
            self.payloads = []
            self.accepted = False

        def export(self, payload):
            self.payloads.append(payload)
            return {"accepted": self.accepted, "rejected_spans": 0 if self.accepted else 1}

    receiver = Receiver()
    with repository(pg.runner_dsn) as repo:
        assert api.export_pending(repo, receiver, limit=10)["sent"] == 0
        receiver.accepted = True
        assert api.export_pending(repo, receiver, limit=10)["sent"] == 1
        assert receiver.payloads[0] == receiver.payloads[1]
        assert api.export_pending(repo, receiver, limit=10)["sent"] == 0


def test_pending_note_cannot_close_and_late_call_after_closure_is_rejected(pg):
    from test_v1_graph_workflow import execute, prepared

    repo, task, _, settings, _ = prepared(pg)
    with repo:
        execute(repo, task, settings)
        api = outbox()
        assert hasattr(api, "collect_run"), "persisted source collector is missing"
        assert hasattr(api, "close_scope"), "guarded scope closure is missing"
        api.collect_run(repo, task["tenant_id"], task["run_id"])
        with pytest.raises(ValueError, match="pending"):
            api.close_scope(
                repo, **{k: task[k] for k in ("tenant_id", "run_id", "task_id")}, scope="online"
            )


def test_automatic_scope_closure_reconciles_calls_and_rejects_new_dispatch(pg):
    import psycopg
    from psycopg.types.json import Jsonb
    from test_v1_graph_workflow import execute, prepared

    repo, task, _, settings, _ = prepared(pg, probability="0.001")
    with repo:
        execute(repo, task, settings)
        api = outbox()
        assert hasattr(api, "close_scope"), "guarded scope closure is missing"
        api.collect_run(repo, task["tenant_id"], task["run_id"])
        api.close_scope(
            repo, **{k: task[k] for k in ("tenant_id", "run_id", "task_id")}, scope="online"
        )
        old = repo._connection.execute("SELECT * FROM reckoner.v1_provider_calls").fetchone()
        new = old["document"] | {"call_id": "late-distinct-call"}
        with pytest.raises(psycopg.errors.CheckViolation, match="closed"):
            repo._connection.execute(
                "INSERT INTO reckoner.v1_provider_calls (tenant_id,call_id,protocol_id,provid"
                "er,run_id,task_id,purpose,maximum_cost,document) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                tuple(
                    new.get(k, old[k])
                    for k in (
                        "tenant_id",
                        "call_id",
                        "protocol_id",
                        "provider",
                        "run_id",
                        "task_id",
                        "purpose",
                        "maximum_cost",
                    )
                )
                + (Jsonb(new),),
            )


def test_evaluator_emits_domain_outcomes_without_runner_oracle_access(pg):
    import psycopg
    from test_v1_graph_workflow import execute, prepared

    repo, task, _, settings, _ = prepared(pg, probability="0.001")
    with repo:
        execute(repo, task, settings)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            outbox().evaluate_run(repo, task["tenant_id"], task["run_id"])
    with repository(pg.evaluator_dsn) as evaluator:
        assert outbox().evaluate_run(evaluator, task["tenant_id"], task["run_id"]) == 4
        assert outbox().evaluate_run(evaluator, task["tenant_id"], task["run_id"]) == 0


def test_review_source_bytes_preserved_and_late_mapping_idempotent(pg):
    from test_v1_graph_workflow import execute, prepared

    repo, task, _, settings, _ = prepared(pg)
    with repo:
        decision, _ = execute(repo, task, settings)
        # Review procedure is the actual Task8 source producer.
        with repository(pg.api_dsn) as api:
            api._connection.execute(
                "SELECT reckoner.v1_review(%s,%s,%s,%s,%s,%s,%s)",
                (
                    task["tenant_id"],
                    decision["decision_id"],
                    "reviewer-task10",
                    "approve",
                    "key-task10",
                    1,
                    False,
                ),
            )
        before = repo._connection.execute(
            "SELECT event_id,document,payload FROM reckoner.v1_outbox"
        ).fetchone()
        outbox().collect_run(repo, task["tenant_id"], task["run_id"])
        outbox().collect_run(repo, task["tenant_id"], task["run_id"])
        assert (
            repo._connection.execute(
                "SELECT event_id,document,payload FROM reckoner.v1_outbox"
            ).fetchone()
            == before
        )
        mapped = repo._connection.execute(
            "SELECT document FROM reckoner.v1_otlp_delivery WHERE event_id=%s",
            (before["event_id"],),
        ).fetchone()["document"]
        assert mapped["payload"]["agreement"] is None
        assert mapped["payload"]["recommendation"] is None


@pytest.mark.parametrize("first", ["closure", "admission"])
def test_call_admission_and_closure_serialize_in_both_orders(pg, first):
    import time
    from concurrent.futures import ThreadPoolExecutor

    import psycopg
    from psycopg.types.json import Jsonb
    from test_v1_graph_workflow import execute, prepared

    repo, task, _, settings, _ = prepared(pg, probability="0.001")
    with repo:
        execute(repo, task, settings)
        old = repo._connection.execute("SELECT * FROM reckoner.v1_provider_calls").fetchone()
        new = old["document"] | {"call_id": "racing-new-call"}

        def admit(other):
            other._connection.execute(
                "INSERT INTO reckoner.v1_provider_calls (tenant_id,call_id,protocol_id,provid"
                "er,run_id,task_id,purpose,maximum_cost,document) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                tuple(
                    new.get(k, old[k])
                    for k in (
                        "tenant_id",
                        "call_id",
                        "protocol_id",
                        "provider",
                        "run_id",
                        "task_id",
                        "purpose",
                        "maximum_cost",
                    )
                )
                + (Jsonb(new),),
            )

        def close(other):
            return outbox().close_scope(
                other, **{k: task[k] for k in ("tenant_id", "run_id", "task_id")}, scope="online"
            )

        def second():
            with repository(pg.runner_dsn) as other:
                other._connection.execute("SET lock_timeout='5s'")
                return admit(other) if first == "closure" else close(other)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with repo._connection.transaction():
                repo._connection.execute(
                    "SELECT task_id FROM reckoner.v1_tasks WHERE tenant_id=%s AND run_id=%s "
                    "AND task_id=%s FOR UPDATE",
                    tuple(task[k] for k in ("tenant_id", "run_id", "task_id")),
                )
                close(repo) if first == "closure" else admit(repo)
                pending = pool.submit(second)
                time.sleep(0.2)
                assert not pending.done(), "competing operation must wait for the task lock"
            expected = psycopg.errors.CheckViolation if first == "closure" else ValueError
            with pytest.raises(expected, match="closed|billing"):
                pending.result(timeout=10)


def test_collector_interruption_restart_and_otlp_receipt(pg):
    import os
    import subprocess
    import time
    from datetime import UTC, datetime

    import clickhouse_connect
    from reckoner.v1.telemetry.exporter import OTLPExporter
    from touchstone_platform.extract import iter_declarations

    endpoint = os.environ.get("RECKONER_TEST_OTLP_ENDPOINT")
    if not endpoint:
        pytest.skip("requires dedicated Task10 Collector fixture")
    assert endpoint.startswith("http://127.0.0.1:"), "disposable loopback only"
    config, manifest, _ = setup_run(pg)
    exporter = OTLPExporter(endpoint)
    with repository(pg.runner_dsn) as repo:
        before = bytes(
            repo._connection.execute("SELECT payload FROM reckoner.v1_otlp_delivery").fetchone()[
                "payload"
            ]
        )
        subprocess.run(
            ["docker", "stop", "touchstone-phase3-task10-collector-1"],
            check=True,
            capture_output=True,
        )
        try:
            assert outbox().export_pending(repo, exporter, limit=10) == {
                "sent": 0,
                "pending": 1,
                "rejected_spans": 0,
            }
        finally:
            subprocess.run(
                ["docker", "start", "touchstone-phase3-task10-collector-1"],
                check=True,
                capture_output=True,
            )
        for _ in range(30):
            result = outbox().export_pending(repo, exporter, limit=10)
            if result["sent"]:
                break
            time.sleep(0.2)
        assert result == {"sent": 1, "pending": 0, "rejected_spans": 0}
        assert (
            bytes(
                repo._connection.execute(
                    "SELECT payload FROM reckoner.v1_otlp_delivery"
                ).fetchone()["payload"]
            )
            == before
        )
        # Lost producer acknowledgement can resend exact bytes; extraction preserves both receipts.
        assert exporter.export(before)["accepted"] is True
    client = clickhouse_connect.get_client(
        host="127.0.0.1",
        port=int(os.environ["RECKONER_TEST_CLICKHOUSE_PORT"]),
        username="touchstone",
        password="fabricated-task10-local",
    )
    try:
        for _ in range(40):
            declarations = list(iter_declarations(client, through=datetime.now(UTC)))
            matching = [
                d
                for d in declarations
                if hasattr(d, "document") and d.document["run_id"] == manifest["run_id"]
            ]
            if len(matching) >= 2:
                break
            time.sleep(0.25)
        assert len(matching) >= 2
        assert len({d.content_sha256 for d in matching}) == 1
        assert matching[0].document["lifecycle_version"] == "root-work-v1"
    finally:
        client.close()


def test_http_partial_success_is_not_delivery_acknowledgement(pg):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
    from reckoner.v1.telemetry.exporter import OTLPExporter

    setup_run(pg)
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            result = ExportTraceServiceResponse()
            if len(received) == 1:
                result.partial_success.rejected_spans = 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(result.SerializeToString())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        exporter = OTLPExporter(f"http://127.0.0.1:{server.server_port}")
        with repository(pg.runner_dsn) as repo:
            assert outbox().export_pending(repo, exporter, limit=1) == {
                "sent": 0,
                "pending": 1,
                "rejected_spans": 1,
            }
            assert outbox().export_pending(repo, exporter, limit=1) == {
                "sent": 1,
                "pending": 0,
                "rejected_spans": 0,
            }
        assert received[0] == received[1]
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


def test_offline_scoring_without_routing_decision_exports_and_closes_only_after_protocol(pg):
    from reckoner.v1.storage.attempts import score_task
    from reckoner.v1.storage.budget import ProviderBudget
    from test_v1_budget import scoring
    from v1_fixtures import identified

    repo, task, evidence, protocol, _, jev = scoring(pg, attempts=1)
    import httpx
    from test_v1_jev import response

    client = jev.JevClient(
        "fabricated",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response())),
    )
    with repo:
        run = repo._connection.execute(
            "SELECT document FROM reckoner.v1_runs WHERE tenant_id=%s AND run_id=%s",
            (task["tenant_id"], task["run_id"]),
        ).fetchone()["document"]
        run = run | {"run_id": "offline-score-only", "purpose": "calibration"}
        identified(run, "experiment_id")
        with repository(pg.owner_dsn) as owner:
            owner.create_run(run, run["config_id"], telemetry_mode="scoring-only")
        task = repo.task(task["tenant_id"], run["run_id"], task["task_id"])
        protocol = protocol | {"run_id": run["run_id"], "purpose": "calibration"}
        identified(protocol, "protocol_id")
        score_task(repo, client, task, evidence, protocol)
        api = outbox()
        with pytest.raises(ValueError, match="mode"):
            api.collect_run(repo, task["tenant_id"], task["run_id"])
        assert hasattr(api, "collect_scoring_run"), "offline score-only telemetry missing"
        api.collect_scoring_run(repo, task["tenant_id"], task["run_id"])
        with pytest.raises(ValueError, match="protocol"):
            api.close_scope(
                repo, **{k: task[k] for k in ("tenant_id", "run_id", "task_id")}, scope="offline"
            )
        ProviderBudget(repo._connection).close(protocol["protocol_id"])
        api.collect_scoring_run(repo, task["tenant_id"], task["run_id"])
        api.close_scope(
            repo, **{k: task[k] for k in ("tenant_id", "run_id", "task_id")}, scope="online"
        )
        api.close_scope(
            repo, **{k: task[k] for k in ("tenant_id", "run_id", "task_id")}, scope="offline"
        )
        docs = [
            r["document"]
            for r in repo._connection.execute(
                "SELECT document FROM reckoner.v1_otlp_delivery WHERE run_id=%s", (task["run_id"],)
            ).fetchall()
        ]
        assert (
            repo._connection.execute("SELECT count(*) AS n FROM reckoner.v1_decisions").fetchone()[
                "n"
            ]
            == 0
        )
        assert not any(d.get("event_kind") == "outcome" for d in docs)
        assert (
            next(d for d in docs if d.get("event_kind") == "provider_usage")["payload"][
                "cost_scope"
            ]
            == "offline"
        )
        assert len([d for d in docs if d.get("event_kind") == "work_closure"]) == 2

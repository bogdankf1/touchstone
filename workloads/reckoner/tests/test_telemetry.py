from __future__ import annotations

import hashlib
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import pytest
from reckoner.contracts import validate_document
from reckoner.storage.postgres import PostgresRepository
from test_runner import FakeProvider, _create_four_task_run

pytestmark = pytest.mark.integration


def _modules():
    runner = importlib.import_module("reckoner.baseline.runner")
    otlp = importlib.import_module("reckoner.telemetry.otlp")
    protobuf = importlib.import_module("opentelemetry.proto.collector.trace.v1.trace_service_pb2")
    return runner, otlp, protobuf


def _completed_run(pg, run_id: str, *, provider=None, seed_sensitive: bool = False):
    runner, otlp, protobuf = _modules()
    _create_four_task_run(pg, run_id)
    if seed_sensitive:
        with psycopg.connect(pg.owner_dsn) as owner:
            owner.execute(
                """
                UPDATE reckoner.transactions
                SET document = jsonb_set(
                  jsonb_set(document, '{processing_errors}', '["prompt-sentinel"]'::jsonb),
                  '{merchant_location}', '"key-sentinel"'::jsonb
                )
                WHERE (tenant_id, transaction_id) IN (
                  SELECT tenant_id, transaction_id FROM reckoner.tasks WHERE run_id = %s
                )
                """,
                (run_id,),
            )
    provider = provider or FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, run_id)
        runner.execute_run(repo, provider, run_id)
    return otlp, protobuf


def _decoded_requests(directory, manifest, message_type):
    decoded = []
    for item in manifest["requests"]:
        payload = (directory / item["filename"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == item["sha256"]
        request = message_type.ExportTraceServiceRequest()
        request.ParseFromString(payload)
        decoded.append((item, payload, request))
    return decoded


def test_export_round_trip_preserves_real_ids_bytes_and_safe_attributes(pg, tmp_path):
    sensitive_result = FakeProvider().result | {
        "raw_body_debug": "body-sentinel",
        "oracle_debug": "label-sentinel",
    }
    otlp, protobuf = _completed_run(
        pg,
        "otlp-roundtrip",
        provider=FakeProvider(result=sensitive_result),
        seed_sensitive=True,
    )
    first = tmp_path / "first"
    second = tmp_path / "second"
    with PostgresRepository(pg.runner_dsn) as repo:
        manifest = otlp.export_run(repo, "otlp-roundtrip", first)
        duplicate = otlp.export_run(repo, "otlp-roundtrip", second)

    assert manifest == duplicate
    assert manifest["request_count"] == 4
    decoded = _decoded_requests(first, manifest, protobuf)
    for item, payload, request in decoded:
        assert payload == (second / item["filename"]).read_bytes()
        spans = [
            span
            for resource in request.resource_spans
            for scope in resource.scope_spans
            for span in scope.spans
        ]
        assert len(spans) == 2
        by_name = {span.name: span for span in spans}
        task_span = by_name["reckoner.task"]
        provider_span = by_name["reckoner.provider.chat"]
        assert provider_span.parent_span_id == task_span.span_id
        assert provider_span.trace_id == task_span.trace_id
        for span in spans:
            assert len(span.trace_id) == 16
            assert len(span.span_id) == 8
            attributes = {attribute.key: attribute.value for attribute in span.attributes}
            rendered = str(attributes).lower()
            assert "prompt-sentinel" not in rendered
            assert "body-sentinel" not in rendered
            assert "key-sentinel" not in rendered
            assert "label-sentinel" not in rendered
        task_attributes = {attribute.key: attribute.value for attribute in task_span.attributes}
        provider_attributes = {
            attribute.key: attribute.value for attribute in provider_span.attributes
        }
        assert "touchstone.provider_cost" not in task_attributes
        assert provider_attributes["gen_ai.operation.name"].string_value == "chat"
        assert provider_attributes["gen_ai.provider.name"].string_value == "anthropic"
        assert provider_attributes["gen_ai.usage.input_tokens"].int_value == 31
        assert provider_attributes["gen_ai.usage.output_tokens"].int_value == 7
        assert provider_attributes["touchstone.provider_call_mode"].string_value == "fake"
        assert "touchstone.provider.mode" not in provider_attributes
        events = [event for span in spans for event in span.events]
        documents = {}
        schema = importlib.import_module("reckoner.telemetry.events").MEASUREMENT_SCHEMA
        for event in events:
            attributes = {attribute.key: attribute.value for attribute in event.attributes}
            document = json.loads(attributes["touchstone.measurement.json"].string_value)
            validate_document(document, schema)
            documents[document["event_kind"]] = document
        assert set(documents) == {"execution", "provider_usage"}
        assert documents["execution"]["trace_id"] == task_span.trace_id.hex()
        assert documents["provider_usage"]["span_id"] == provider_span.span_id.hex()
        assert item["event_id"]
        assert item["run_id"] == "otlp-roundtrip"


def test_replay_posts_unchanged_protobuf_and_handles_partial_success(pg, tmp_path):
    otlp, protobuf = _completed_run(pg, "otlp-replay")
    output = tmp_path / "export"
    with PostgresRepository(pg.runner_dsn) as repo:
        manifest = otlp.export_run(repo, "otlp-replay", output)
    expected = [payload for _, payload, _ in _decoded_requests(output, manifest, protobuf)]
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            assert self.path == "/v1/traces"
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            response = protobuf.ExportTraceServiceResponse()
            if len(received) == len(expected):
                response.partial_success.rejected_spans = 1
                response.partial_success.error_message = "synthetic rejection"
            body = response.SerializeToString()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = otlp.replay(output, f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        thread.join()
    assert received == expected
    assert result == {"sent": 3, "pending": 1, "rejected_spans": 1}


def test_http_error_retains_all_replay_requests_as_pending(pg, tmp_path):
    otlp, _ = _completed_run(pg, "otlp-http-error")
    output = tmp_path / "export"
    with PostgresRepository(pg.runner_dsn) as repo:
        otlp.export_run(repo, "otlp-http-error", output)

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(503)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = otlp.replay(output, f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        thread.join()
    assert result == {"sent": 0, "pending": 4, "rejected_spans": 0}


def test_disk_export_failure_leaves_outbox_pending_without_regeneration(pg, tmp_path, monkeypatch):
    runner, otlp, _ = _modules()
    _create_four_task_run(pg, "otlp-disk-error")
    provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, "otlp-disk-error")
        runner.execute_run(repo, provider, "otlp-disk-error")
        calls = provider.generation_calls

        def fail_write(*_args, **_kwargs):
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(otlp, "_atomic_write", fail_write)
        with pytest.raises(OSError, match="synthetic disk failure"):
            otlp.export_run(repo, "otlp-disk-error", tmp_path / "export")
        assert {row["status"] for row in repo.outbox_rows("otlp-disk-error")} == {"failed"}
        runner.execute_run(repo, provider, "otlp-disk-error")
    assert provider.generation_calls == calls == 4


def test_outbox_insert_failure_rolls_back_completion_and_leaves_dispatch_uncertain(pg, monkeypatch):
    runner, _, _ = _modules()
    _create_four_task_run(pg, "outbox-failure")
    provider = FakeProvider()
    with PostgresRepository(pg.runner_dsn) as repo:
        runner.preflight(repo, provider, "outbox-failure")

        def fail_export(*_args, **_kwargs):
            raise RuntimeError("synthetic outbox write failure")

        telemetry = importlib.import_module("reckoner.telemetry.events")
        monkeypatch.setattr(telemetry, "store_provider_span", fail_export)
        result = runner.execute_run(repo, provider, "outbox-failure")
        snapshot = repo.snapshot("outbox-failure", None)
    assert provider.generation_calls == 1
    assert result["uncertain"] == 1
    assert snapshot["decisions"] == []
    assert snapshot["attempts"][0]["status"] == "uncertain"


def test_runner_cannot_read_evaluator_outbox_payload(pg):
    _create_four_task_run(pg, "outbox-isolation")
    payload = b"oracle-label-sentinel"
    with psycopg.connect(pg.evaluator_dsn) as evaluator:
        task = evaluator.execute(
            "SELECT tenant_id, run_id, task_id FROM reckoner.tasks "
            "WHERE run_id = 'outbox-isolation' LIMIT 1"
        ).fetchone()
        evaluator.execute(
            "INSERT INTO reckoner.evaluator_telemetry_outbox "
            "(tenant_id, event_id, run_id, task_id, payload, producer) "
            "VALUES (%s,%s,%s,%s,%s,'evaluator')",
            (task[0], "evaluation-secret-event", task[1], task[2], payload),
        )
    with psycopg.connect(pg.runner_dsn) as connection:
        rows = connection.execute("SELECT payload FROM reckoner.runner_telemetry_outbox").fetchall()
        assert all(bytes(row[0]) != payload for row in rows)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT payload FROM reckoner.telemetry_outbox").fetchall()
        connection.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT payload FROM reckoner.evaluator_telemetry_outbox").fetchall()


def test_measurement_events_are_schema_validated_before_encoding():
    events = importlib.import_module("reckoner.telemetry.events")
    with pytest.raises(ValueError, match="measurement"):
        events.encode_measurement_event({"schema_version": "measurement-v1"})


@pytest.mark.parametrize("unsafe", ["../outside.pb", "/tmp/outside.pb"])
def test_replay_rejects_unsafe_manifest_paths_before_any_network(pg, tmp_path, unsafe):
    run_id = f"unsafe-{hashlib.sha256(unsafe.encode()).hexdigest()[:8]}"
    otlp, _ = _completed_run(pg, run_id)
    output = tmp_path / "export"
    with PostgresRepository(pg.runner_dsn) as repo:
        manifest = otlp.export_run(repo, run_id, output)
    manifest["requests"][1]["filename"] = unsafe
    (output / "manifest.json").write_text(json.dumps(manifest))
    calls = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            calls.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(ValueError, match="manifest|path"):
            otlp.replay(output, f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        thread.join()
    assert calls == []


def test_replay_validates_every_checksum_before_any_network(pg, tmp_path):
    otlp, _ = _completed_run(pg, "unsafe-checksum")
    output = tmp_path / "export"
    with PostgresRepository(pg.runner_dsn) as repo:
        manifest = otlp.export_run(repo, "unsafe-checksum", output)
    manifest["requests"][-1]["sha256"] = "0" * 64
    (output / "manifest.json").write_text(json.dumps(manifest))
    calls = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            calls.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(ValueError, match="checksum"):
            otlp.replay(output, f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        thread.join()
    assert calls == []

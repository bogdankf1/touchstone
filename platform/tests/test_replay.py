import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from touchstone_platform.replay import replay_exports


def _manifest(directory, payloads):
    requests = []
    for index, payload in enumerate(payloads, start=1):
        filename = f"{index:04d}-event-{index}.pb"
        (directory / filename).write_bytes(payload)
        requests.append(
            {
                "filename": filename,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "event_id": f"event-{index}",
                "run_id": "run-1",
                "tenant_id": "tenant-a",
                "task_id": f"task-{index}",
            }
        )
    manifest = {
        "schema_version": "otlp-export-manifest-v1",
        "otlp_protocol": "http/protobuf",
        "request_count": len(requests),
        "requests": requests,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def _payload(name):
    request = ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add()
    span.name = name
    span.trace_id = b"\x01" * 16
    span.span_id = b"\x02" * 8
    return request.SerializeToString()


@pytest.fixture
def receiver():
    received = []
    responses = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(
                (
                    self.path,
                    self.headers.get("Content-Type"),
                    self.rfile.read(int(self.headers["Content-Length"])),
                )
            )
            status, payload = responses.pop(0) if responses else (200, b"")
            self.send_response(status)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", received, responses
    server.shutdown()
    thread.join()
    server.server_close()


def test_replay_sends_identical_bytes_twice(tmp_path, receiver):
    endpoint, received, _ = receiver
    one, two = _payload("one"), _payload("two")
    _manifest(tmp_path, [one, two])
    assert replay_exports(tmp_path, endpoint).sent == 2
    assert replay_exports(tmp_path, endpoint).sent == 2
    assert (
        received
        == [
            ("/v1/traces", "application/x-protobuf", one),
            ("/v1/traces", "application/x-protobuf", two),
        ]
        * 2
    )


def test_replay_checks_all_checksums_before_network(tmp_path, receiver):
    endpoint, received, _ = receiver
    manifest = _manifest(tmp_path, [_payload("one"), _payload("two")])
    (tmp_path / manifest["requests"][1]["filename"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        replay_exports(tmp_path, endpoint)
    assert received == []


@pytest.mark.parametrize(
    "unsafe", ["../0001-event-1.pb", "/tmp/0001-event-1.pb", "0001-event-1.pb/../0001-event-1.pb"]
)
def test_replay_rejects_path_escape_before_network(tmp_path, receiver, unsafe):
    endpoint, received, _ = receiver
    manifest = _manifest(tmp_path, [_payload("one")])
    manifest["requests"][0]["filename"] = unsafe
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest|path"):
        replay_exports(tmp_path, endpoint)
    assert received == []


def test_replay_rejects_empty_manifest(tmp_path, receiver):
    endpoint, received, _ = receiver
    _manifest(tmp_path, [])
    with pytest.raises(ValueError, match="empty"):
        replay_exports(tmp_path, endpoint)
    assert received == []


def test_replay_stops_on_partial_rejection(tmp_path, receiver):
    endpoint, received, responses = receiver
    _manifest(tmp_path, [_payload("one"), _payload("two")])
    response = ExportTraceServiceResponse()
    response.partial_success.rejected_spans = 1
    responses.append((200, response.SerializeToString()))
    result = replay_exports(tmp_path, endpoint)
    assert (result.sent, result.pending, result.rejected_spans) == (0, 2, 1)
    assert len(received) == 1


def test_replay_stops_on_malformed_response(tmp_path, receiver):
    endpoint, received, responses = receiver
    _manifest(tmp_path, [_payload("one"), _payload("two")])
    responses.append((200, b"garbage"))
    result = replay_exports(tmp_path, endpoint)
    assert (result.sent, result.pending) == (0, 2)
    assert len(received) == 1


def test_replay_accepts_empty_partial_success(tmp_path, receiver):
    endpoint, _, responses = receiver
    _manifest(tmp_path, [_payload("one")])
    responses.append((200, b"\x0a\x00"))
    assert replay_exports(tmp_path, endpoint).sent == 1

"""Real Collector and ClickHouse persistence smoke; invoked by infra/smoke-platform-ingestion.sh."""

import hashlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import clickhouse_connect
import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from touchstone_platform.extract import ValidatedDeclaration, iter_declarations, iter_measurements
from touchstone_platform.replay import replay_exports

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"
COMPOSE = Path(__file__).resolve().parents[2] / "infra" / "compose.platform.yaml"


def _attribute(attributes, key, value):
    attribute = attributes.add()
    attribute.key = key
    if isinstance(value, bool):
        attribute.value.bool_value = value
    else:
        attribute.value.string_value = value


def _request(document, *, declaration=False):
    request = ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    _attribute(resource.resource.attributes, "service.name", "touchstone-task2-smoke")
    span = resource.scope_spans.add().spans.add()
    span.trace_id = bytes.fromhex("01" * 16)
    span.span_id = bytes.fromhex("03" * 8 if declaration else "02" * 8)
    span.name = "declaration" if declaration else "measurement"
    span.start_time_unix_nano = 1577836800000000000
    span.end_time_unix_nano = 1577836800000000001
    for field in ("tenant_id", "workflow_id", "workflow_version", "run_id"):
        _attribute(span.attributes, f"touchstone.{field}", document[field])
    if not declaration:
        _attribute(span.attributes, "touchstone.task_id", document["task_id"])
        _attribute(span.attributes, "touchstone.simulated", document["simulated"])
    event = span.events.add()
    event.name = (
        "touchstone.run.declaration"
        if declaration
        else f"touchstone.measurement.{document['event_kind']}"
    )
    event.time_unix_nano = span.start_time_unix_nano
    _attribute(
        event.attributes,
        "touchstone.run.json" if declaration else "touchstone.measurement.json",
        json.dumps(document, sort_keys=True, separators=(",", ":")),
    )
    return request.SerializeToString()


def _manifest(directory, payloads):
    requests = []
    for number, payload in enumerate(payloads, start=1):
        filename = f"{number:04d}-smoke-{number}.pb"
        (directory / filename).write_bytes(payload)
        requests.append(
            {
                "filename": filename,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "event_id": f"smoke-{number}",
                "run_id": "run-fixture-1",
                "tenant_id": "tenant-fixture-a",
                "task_id": f"task-{number}",
            }
        )
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "otlp-export-manifest-v1",
                "otlp_protocol": "http/protobuf",
                "request_count": len(requests),
                "requests": requests,
            }
        )
    )


def _compose(*args):
    subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            os.environ["TOUCHSTONE_COMPOSE_PROJECT"],
            "-f",
            str(COMPOSE),
            *args,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def _port(service, internal_port):
    output = subprocess.check_output(
        [
            "docker",
            "compose",
            "-p",
            os.environ["TOUCHSTONE_COMPOSE_PROJECT"],
            "-f",
            str(COMPOSE),
            "port",
            service,
            str(internal_port),
        ],
        text=True,
    )
    return int(output.strip().rsplit(":", 1)[1])


def _client():
    return clickhouse_connect.get_client(
        host="127.0.0.1",
        port=_port("clickhouse", 8123),
        username="touchstone",
        password=os.environ["TOUCHSTONE_CH_PASSWORD"],
    )


def _endpoint():
    return f"http://127.0.0.1:{_port('collector', 4318)}"


def _wait(client, count):
    deadline = time.monotonic() + 90
    last_error = None
    while time.monotonic() < deadline:
        try:
            rows = client.query("SELECT count() FROM otel.otel_traces").result_rows[0][0]
            if rows >= count:
                return rows
        except Exception as error:
            last_error = error
        time.sleep(1)
    pytest.fail(f"ClickHouse never reached {count} raw spans: {last_error}")


@pytest.mark.integration
def test_actual_collector_queue_restart_and_logical_replay(tmp_path):
    project = os.environ.get("TOUCHSTONE_COMPOSE_PROJECT")
    if not project:
        pytest.skip("run via infra/smoke-platform-ingestion.sh")
    client = _client()
    measurement = json.loads((EXAMPLES / "measurement-v1.json").read_text())
    measurement["trace_id"] = "01" * 16
    measurement["span_id"] = "02" * 8
    declaration = json.loads((EXAMPLES / "run-declaration-v1.json").read_text())
    _manifest(tmp_path, [_request(measurement), _request(declaration, declaration=True)])

    assert replay_exports(tmp_path, _endpoint()).sent == 2
    first_count = _wait(client, 2)
    assert client.query("SELECT DISTINCT tenant_id FROM otel.otel_traces").result_rows == [
        ("tenant-fixture-a",)
    ]
    cutoff = datetime.now(UTC)
    first = list(iter_measurements(client, through=cutoff, page_size=1))
    first_declarations = list(iter_declarations(client, through=cutoff, page_size=1))
    assert len(first) == 1 and first[0].document["event_id"] == "measurement-fixture-1"
    assert len(first_declarations) == 1
    assert isinstance(first_declarations[0], ValidatedDeclaration)
    assert first[0].received_at.startswith("2026-")
    assert first[0].document["occurred_at"].startswith("2020-")

    _compose("stop", "clickhouse")
    assert replay_exports(tmp_path, _endpoint()).sent == 2  # accepted into durable queue
    _compose("restart", "collector")
    _compose("up", "-d", "--wait", "clickhouse")
    client = _client()  # Docker reassigns the host's ephemeral port on start.
    queued_count = _wait(client, first_count + 2)
    assert replay_exports(tmp_path, _endpoint()).sent == 2
    final_count = _wait(client, queued_count + 2)
    final_cutoff = datetime.now(UTC)
    events = list(iter_measurements(client, through=final_cutoff, page_size=1))
    declarations = list(iter_declarations(client, through=final_cutoff, page_size=1))
    assert final_count >= 6
    assert {item.content_sha256 for item in events} == {first[0].content_sha256}
    assert {item.content_sha256 for item in declarations} == {first_declarations[0].content_sha256}
    assert len(events) >= 3 and len(declarations) >= 3

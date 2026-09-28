"""Build replay declarations from frozen identities and check membership only."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from touchstone_platform.contracts import validate_declaration


def _source(directory: Path):
    root = directory.resolve()
    manifest_bytes = (root / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest["request_count"] != len(manifest["requests"]):
        raise ValueError("inconsistent source manifest")
    for item in manifest["requests"]:
        path = (root / item["filename"]).resolve()
        if path.parent != root:
            raise ValueError("source path escapes manifest directory")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != item["sha256"]:
            raise ValueError("source checksum mismatch")
        yield (
            item,
            ExportTraceServiceRequest.FromString(payload),
            hashlib.sha256(manifest_bytes).hexdigest(),
        )


def _documents(request):
    for resource in request.resource_spans:
        for scope in resource.scope_spans:
            for span in scope.spans:
                for event in span.events:
                    for attribute in event.attributes:
                        if attribute.key == "touchstone.measurement.json":
                            yield json.loads(attribute.value.string_value)


def build_declarations(
    runner_dir: Path, evaluator_dir: Path, expectations_path: Path
) -> list[dict]:
    """Expand independently pinned expectations over frozen runner task IDs."""
    expectations = json.loads(expectations_path.read_text())
    if expectations.get("schema_version") != "phase1-replay-expectations-v1":
        raise ValueError("unsupported replay expectations")
    tasks = defaultdict(set)
    metadata = {}
    source_hash = hashlib.sha256((runner_dir / "manifest.json").read_bytes()).hexdigest()
    evaluator_hash = hashlib.sha256((evaluator_dir / "manifest.json").read_bytes()).hexdigest()
    for item, request, digest in _source(runner_dir):
        if digest != source_hash:
            raise ValueError("runner manifest changed during preflight")
        key = (item["run_id"], item["tenant_id"])
        tasks[key].add(item["task_id"])
        for document in _documents(request):
            metadata.setdefault(key, document)
    if not tasks:
        raise ValueError("empty runner export")
    evaluator_tasks = defaultdict(set)
    for item, _, digest in _source(evaluator_dir):
        if digest != evaluator_hash:
            raise ValueError("evaluator manifest changed during preflight")
        key = (item["run_id"], item["tenant_id"])
        evaluator_tasks[key].add(item["task_id"])
    if any(
        key not in tasks or not members <= tasks[key] for key, members in evaluator_tasks.items()
    ):
        raise ValueError("evaluator has unexpected task membership")
    run_ids = {run_id for run_id, _ in tasks}
    source_pins = [
        item
        for item in expectations["sources"]
        if item["runner_manifest_sha256"] == source_hash
        and item["evaluator_manifest_sha256"] == evaluator_hash
    ]
    if len(source_pins) != 1 or run_ids != {source_pins[0]["run_id"]}:
        raise ValueError("expectation source pin does not match frozen exports")
    declarations = []
    for (run_id, tenant_id), members in sorted(tasks.items()):
        source = metadata[(run_id, tenant_id)]
        if (
            source["workflow_id"] != expectations["workflow_id"]
            or source["workflow_version"] != expectations["workflow_version"]
        ):
            raise ValueError("expectation workflow does not match frozen exports")
        versions = source["reproducibility"]
        event_id = hashlib.sha256(f"{source_hash}:{run_id}:{tenant_id}".encode()).hexdigest()
        document = {
            "schema_version": "run-declaration-v1",
            "event_id": event_id,
            "tenant_id": tenant_id,
            "workflow_id": source["workflow_id"],
            "workflow_version": source["workflow_version"],
            "run_id": run_id,
            "declared_at": source["occurred_at"],
            "expected_task_count": len(members),
            "expected_task_ids": sorted(members),
            "experiment_version": versions["experiment_version"],
            "cohort_version": versions["cohort_version"],
            "config_version": versions["config_version"],
            "code_revision": versions["code_revision"],
            "dataset_version": versions["dataset_version"],
            "measurement_mode": "measured",
            "dataset_simulated": True,
            "replay": {"is_replay": True, "source_manifest_sha256": source_hash},
            "evaluation_suites": [],
            "metric_expectations": [],
        }
        for suite in expectations["evaluation_suites"]:
            if suite["case_membership"] != "all_runner_tasks":
                raise ValueError("unsupported evaluation case membership")
            document["evaluation_suites"].append(
                {
                    "suite_id": suite["suite_id"],
                    "suite_version": suite["suite_version"],
                    "expected_case_ids": sorted(members),
                    "required_checks": suite["required_checks"],
                }
            )
        for metric in expectations["metric_expectations"]:
            if metric["task_membership"] != "all_runner_tasks":
                raise ValueError("unsupported metric task membership")
            document["metric_expectations"].append(
                {
                    "metric_id": metric["metric_id"],
                    "definition_version": metric["definition_version"],
                    "unit": metric["unit"],
                    "expected_task_ids": sorted(members),
                }
            )
        validate_declaration(document)
        declarations.append(document)
    return declarations


def encode_declaration(document: dict) -> bytes:
    request = ExportTraceServiceRequest()
    resource = request.resource_spans.add()
    attribute = resource.resource.attributes.add()
    attribute.key = "service.name"
    attribute.value.string_value = "touchstone-phase1-replay"
    span = resource.scope_spans.add().spans.add()
    span.name = "touchstone.replay.declaration"
    span.trace_id = bytes.fromhex(hashlib.sha256(document["event_id"].encode()).hexdigest()[:32])
    span.span_id = bytes.fromhex(document["event_id"][:16])
    instant = int(
        datetime.fromisoformat(document["declared_at"].replace("Z", "+00:00"))
        .astimezone(UTC)
        .timestamp()
        * 1_000_000_000
    )
    span.start_time_unix_nano = instant
    span.end_time_unix_nano = instant + 1_000_000
    for name in ("tenant_id", "workflow_id", "workflow_version", "run_id"):
        attribute = span.attributes.add()
        attribute.key = f"touchstone.{name}"
        attribute.value.string_value = document[name]
    attribute = span.attributes.add()
    attribute.key = "touchstone.dataset_simulated"
    attribute.value.bool_value = True
    event = span.events.add()
    event.name = "touchstone.run.declaration"
    event.time_unix_nano = instant
    attribute = event.attributes.add()
    attribute.key = "touchstone.run.json"
    attribute.value.string_value = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return request.SerializeToString(deterministic=True)

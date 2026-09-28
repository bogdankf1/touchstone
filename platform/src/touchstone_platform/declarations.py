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


def build_declarations(runner_dir: Path, evaluator_dir: Path) -> list[dict]:
    """Read only membership, version identities and required check names."""
    tasks = defaultdict(set)
    metadata = {}
    suites = defaultdict(lambda: defaultdict(lambda: {"cases": set(), "checks": set()}))
    metrics = defaultdict(lambda: defaultdict(set))
    source_hash = None
    for item, request, digest in _source(runner_dir):
        source_hash = digest
        key = (item["run_id"], item["tenant_id"])
        tasks[key].add(item["task_id"])
        for document in _documents(request):
            metadata.setdefault(key, document)
    if not tasks:
        raise ValueError("empty runner export")
    evaluator_tasks = defaultdict(set)
    for item, request, _ in _source(evaluator_dir):
        key = (item["run_id"], item["tenant_id"])
        evaluator_tasks[key].add(item["task_id"])
        for document in _documents(request):
            if document["event_kind"] == "evaluation":
                payload = document["payload"]
                suite = suites[key][payload["suite_id"]]
                suite["cases"].add(payload["case_id"])
                suite["checks"].add(payload["metric_id"])
            elif document["event_kind"] == "metric_contribution":
                payload = document["payload"]
                metrics[key][
                    (payload["metric_id"], payload["definition_version"], payload["unit"])
                ].add(item["task_id"])
    if dict(tasks) != dict(evaluator_tasks):
        raise ValueError("runner and evaluator task membership differ")
    declarations = []
    for (run_id, tenant_id), members in sorted(tasks.items()):
        source = metadata[(run_id, tenant_id)]
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
            "evaluation_suites": [
                {
                    "suite_id": suite_id,
                    "suite_version": "v1",
                    "expected_case_ids": sorted(details["cases"]),
                    "required_checks": sorted(details["checks"]),
                }
                for suite_id, details in sorted(suites[(run_id, tenant_id)].items())
            ],
            "metric_expectations": [
                {
                    "metric_id": metric_id,
                    "definition_version": version,
                    "unit": unit,
                    "expected_task_ids": sorted(metric_tasks),
                }
                for (metric_id, version, unit), metric_tasks in sorted(
                    metrics[(run_id, tenant_id)].items()
                )
            ],
        }
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

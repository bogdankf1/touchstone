"""Atomic local export and byte-preserving OTLP/HTTP replay."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)

from reckoner.contracts import validate_document
from reckoner.resources import SCHEMAS

MANIFEST_SCHEMA = SCHEMAS / "otlp-export-manifest-v1.schema.json"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _export(repo, run_id: str, output: Path, *, evaluator: bool) -> dict[str, Any]:
    output = Path(output)
    rows = repo.evaluation_outbox_rows(run_id) if evaluator else repo.outbox_rows(run_id)
    event_ids = [row["event_id"] for row in rows]
    requests = []
    try:
        for index, row in enumerate(rows, start=1):
            payload = bytes(row["payload"])
            filename = f"{index:04d}-{row['event_id']}.pb"
            _atomic_write(output / filename, payload)
            requests.append(
                {
                    "filename": filename,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "event_id": row["event_id"],
                    "run_id": row["run_id"],
                    "tenant_id": row["tenant_id"],
                    "task_id": row["task_id"],
                }
            )
        manifest = {
            "schema_version": "otlp-export-manifest-v1",
            "otlp_protocol": "http/protobuf",
            "request_count": len(requests),
            "requests": requests,
        }
        validate_document(manifest, MANIFEST_SCHEMA)
        _atomic_write(
            output / "manifest.json",
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
    except BaseException:
        if evaluator:
            repo.set_evaluation_outbox_status(run_id, event_ids, "failed")
        else:
            repo.set_outbox_status(run_id, event_ids, "failed")
        raise
    if evaluator:
        repo.set_evaluation_outbox_status(run_id, event_ids, "exported")
    else:
        repo.set_outbox_status(run_id, event_ids, "exported")
    return manifest


def export_run(repo, run_id: str, output: Path) -> dict[str, Any]:
    """Export runner evidence using only the runner-filtered outbox view."""
    return _export(repo, run_id, output, evaluator=False)


def export_evaluations(repo, run_id: str, output: Path) -> dict[str, Any]:
    """Export evaluator evidence using only the evaluator-filtered outbox view."""
    return _export(repo, run_id, output, evaluator=True)


def _load_manifest(artifact_dir: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid OTLP export manifest") from error
    try:
        validate_document(manifest, MANIFEST_SCHEMA)
    except Exception as error:
        raise ValueError("invalid OTLP export manifest") from error
    if manifest["request_count"] != len(manifest["requests"]):
        raise ValueError("invalid OTLP export manifest")
    return manifest


def _validated_payloads(artifact_dir: Path, manifest: dict[str, Any]) -> list[bytes]:
    root = artifact_dir.resolve()
    payloads = []
    for item in manifest["requests"]:
        path = (root / item["filename"]).resolve()
        if path.parent != root:
            raise ValueError("OTLP manifest path escapes artifact directory")
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise ValueError("invalid OTLP export manifest path") from error
        if hashlib.sha256(payload).hexdigest() != item["sha256"]:
            raise ValueError("OTLP export checksum mismatch")
        payloads.append(payload)
    return payloads


def replay(artifact_dir: Path, endpoint: str) -> dict[str, int]:
    """POST exact stored request bytes and account for OTLP partial success."""
    artifact_dir = Path(artifact_dir)
    manifest = _load_manifest(artifact_dir)
    requests = manifest["requests"]
    payloads = _validated_payloads(artifact_dir, manifest)
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url += "/v1/traces"
    sent = 0
    rejected = 0
    for payload in payloads:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/x-protobuf"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status < 200 or response.status >= 300:
                    break
                body = response.read()
        except (OSError, urllib.error.HTTPError, urllib.error.URLError):
            break
        decoded = ExportTraceServiceResponse()
        try:
            decoded.ParseFromString(body)
        except Exception:
            break
        rejected_here = decoded.partial_success.rejected_spans
        if rejected_here:
            rejected += int(rejected_here)
            return {
                "sent": sent,
                "pending": len(requests) - sent,
                "rejected_spans": rejected,
            }
        sent += 1
    return {
        "sent": sent,
        "pending": len(requests) - sent,
        "rejected_spans": rejected,
    }

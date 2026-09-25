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


def export_run(repo, run_id: str, output: Path) -> dict[str, Any]:
    """Write deterministic OTLP request files plus a checksum manifest."""
    output = Path(output)
    rows = repo.outbox_rows(run_id)
    requests = []
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
    _atomic_write(
        output / "manifest.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return manifest


def _load_manifest(artifact_dir: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((artifact_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid OTLP export manifest") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "otlp-export-manifest-v1"
    ):
        raise ValueError("invalid OTLP export manifest")
    return manifest


def replay(artifact_dir: Path, endpoint: str) -> dict[str, int]:
    """POST exact stored request bytes and account for OTLP partial success."""
    artifact_dir = Path(artifact_dir)
    manifest = _load_manifest(artifact_dir)
    requests = manifest.get("requests")
    if not isinstance(requests, list):
        raise ValueError("invalid OTLP export manifest")
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url += "/v1/traces"
    sent = 0
    rejected = 0
    for item in requests:
        try:
            payload = (artifact_dir / item["filename"]).read_bytes()
        except (KeyError, OSError, TypeError) as error:
            raise ValueError("invalid OTLP export request") from error
        if hashlib.sha256(payload).hexdigest() != item.get("sha256"):
            raise ValueError("OTLP export checksum mismatch")
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

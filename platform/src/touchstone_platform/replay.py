"""Byte-preserving replay of frozen OTLP trace exports."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

from touchstone_platform.resources import SCHEMAS

_SCHEMA = json.loads(SCHEMAS.joinpath("otlp-export-manifest-v1.schema.json").read_text())
_VALIDATOR = Draft202012Validator(_SCHEMA)


@dataclass(frozen=True)
class ReplayResult:
    sent: int
    pending: int
    rejected_spans: int


def _validated_payloads(manifest_dir: Path) -> list[bytes]:
    root = manifest_dir.resolve()
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        _VALIDATOR.validate(manifest)
    except Exception as error:
        raise ValueError("invalid OTLP export manifest") from error
    requests = manifest["requests"]
    if not requests or manifest["request_count"] != len(requests):
        raise ValueError("empty or inconsistent OTLP export manifest")
    payloads = []
    for item in requests:
        path = (root / item["filename"]).resolve()
        if path.parent != root:
            raise ValueError("OTLP manifest path escapes directory")
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise ValueError("invalid OTLP export manifest path") from error
        if hashlib.sha256(payload).hexdigest() != item["sha256"]:
            raise ValueError("OTLP export checksum mismatch")
        message = ExportTraceServiceRequest()
        try:
            message.ParseFromString(payload)
        except Exception as error:
            raise ValueError("malformed OTLP request") from error
        if not message.resource_spans:
            raise ValueError("empty OTLP request")
        payloads.append(payload)
    return payloads


def replay_exports(manifest_dir: Path, endpoint: str) -> ReplayResult:
    """Validate the entire manifest before posting any original request bytes."""
    payloads = _validated_payloads(Path(manifest_dir))
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url += "/v1/traces"
    sent = 0
    for payload in payloads:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/x-protobuf"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if not 200 <= response.status < 300:
                    break
                body = response.read()
        except (OSError, urllib.error.HTTPError, urllib.error.URLError):
            break
        decoded = ExportTraceServiceResponse()
        try:
            decoded.ParseFromString(body)
        except Exception:
            break
        if decoded.HasField("partial_success"):
            rejected = int(decoded.partial_success.rejected_spans)
            if rejected or decoded.partial_success.error_message:
                return ReplayResult(sent, len(payloads) - sent, rejected)
        sent += 1
    return ReplayResult(sent, len(payloads) - sent, 0)

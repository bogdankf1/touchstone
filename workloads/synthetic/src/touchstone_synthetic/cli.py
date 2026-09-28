"""Send the fabricated workflow to a local OTLP/HTTP collector."""

from __future__ import annotations

import argparse
import json
import urllib.request

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from touchstone_synthetic.events import build_requests


def main() -> int:
    parser = argparse.ArgumentParser(prog="touchstone-synthetic")
    command = parser.add_subparsers(dest="command", required=True)
    emit = command.add_parser("emit", help="emit clearly fabricated workflow traces")
    emit.add_argument("--endpoint", required=True)
    emit.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if args.command != "emit":
        return 2
    url = args.endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url += "/v1/traces"
    sent = 0
    for payload in build_requests(args.run_id) + build_requests(args.run_id, incomplete=True):
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/x-protobuf"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            reply = ExportTraceServiceResponse.FromString(response.read())
            if response.status != 200 or reply.partial_success.rejected_spans:
                raise RuntimeError("collector rejected synthetic spans")
        sent += 1
    print(json.dumps({"sent_requests": sent, "run_id": args.run_id, "fabricated": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

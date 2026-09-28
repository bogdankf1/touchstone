"""Touchstone operator commands."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from touchstone_platform.declarations import build_declarations, encode_declaration
from touchstone_platform.refresh import refresh
from touchstone_platform.replay import replay_exports
from touchstone_platform.settings import Settings
from touchstone_platform.verify import verify_published


def main() -> int:
    parser = argparse.ArgumentParser(prog="touchstone")
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser("replay", help="replay a frozen OTLP export")
    replay.add_argument("--manifest-dir", type=Path, required=True)
    replay.add_argument("--endpoint", required=True)
    verify = commands.add_parser(
        "verify", help="compare a published run with an acceptance receipt"
    )
    verify.add_argument("--expected", type=Path, required=True)
    verify.add_argument("--run-id", required=True)
    declare = commands.add_parser("declare", help="prepare and emit Phase 1 replay declarations")
    declare.add_argument("--runner-dir", type=Path, required=True)
    declare.add_argument("--evaluator-dir", type=Path, required=True)
    declare.add_argument("--output-dir", type=Path, required=True)
    declare.add_argument("--endpoint", required=True)
    commands.add_parser("refresh", help="build and publish a verified local warehouse snapshot")
    serve = commands.add_parser("serve", help="serve the read-only dashboard API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "replay":
        result = replay_exports(args.manifest_dir, args.endpoint)
        print(json.dumps(vars(result), sort_keys=True))
        return 0 if result.pending == 0 and result.rejected_spans == 0 else 1
    if args.command == "refresh":
        result = refresh(Settings.from_env())
        print(json.dumps(vars(result), sort_keys=True))
        return 0
    if args.command == "verify":
        expected = json.loads(args.expected.read_text())
        generation, mismatches = verify_published(expected, args.run_id, Settings.from_env())
        print(
            json.dumps(
                {"generation": generation, "run_id": args.run_id, "mismatches": mismatches},
                sort_keys=True,
            )
        )
        return 1 if mismatches else 0
    if args.command == "declare":
        documents = build_declarations(args.runner_dir, args.evaluator_dir)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        endpoint = args.endpoint.rstrip("/")
        if not endpoint.endswith("/v1/traces"):
            endpoint += "/v1/traces"
        for document in documents:
            name = f"{document['run_id']}-{document['tenant_id']}"
            (args.output_dir / f"{name}.json").write_text(
                json.dumps(document, sort_keys=True, indent=2)
            )
            payload = encode_declaration(document)
            (args.output_dir / f"{name}.pb").write_bytes(payload)
            request = urllib.request.Request(
                endpoint,
                data=payload,
                method="POST",
                headers={"Content-Type": "application/x-protobuf"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                reply = ExportTraceServiceResponse.FromString(response.read())
                if response.status != 200 or reply.partial_success.rejected_spans:
                    raise RuntimeError("collector rejected declaration")
        print(json.dumps({"declarations": len(documents), "run_id": documents[0]["run_id"]}))
        return 0
    if args.command == "serve":
        import uvicorn

        from touchstone_platform.api import create_app

        uvicorn.run(create_app(Settings.from_env()), host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

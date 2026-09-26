"""Touchstone operator commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from touchstone_platform.refresh import refresh
from touchstone_platform.replay import replay_exports
from touchstone_platform.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(prog="touchstone")
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser("replay", help="replay a frozen OTLP export")
    replay.add_argument("--manifest-dir", type=Path, required=True)
    replay.add_argument("--endpoint", required=True)
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
    if args.command == "serve":
        import uvicorn

        from touchstone_platform.api import create_app

        uvicorn.run(create_app(Settings.from_env()), host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

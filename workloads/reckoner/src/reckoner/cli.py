"""Command-line entry point for Reckoner batch operations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from reckoner.data.artifacts import verify_bundle
from reckoner.data.cohort import prepare


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reckoner")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--source-dir", type=Path, required=True)
    prepare_command.add_argument("--output", type=Path, required=True)
    verify_command = commands.add_parser("verify")
    verify_command.add_argument("--artifact-dir", type=Path, required=True)
    verify_command.add_argument("--source-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            index = prepare(args.source_dir, args.output)
        else:
            index = verify_bundle(args.artifact_dir, args.source_dir)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"bundle_id": index["bundle_id"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

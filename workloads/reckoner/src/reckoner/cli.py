"""Command-line entry point for Reckoner batch operations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

from reckoner.data.artifacts import verify_bundle
from reckoner.data.cohort import prepare
from reckoner.storage.migrate import migrate
from reckoner.storage.postgres import PostgresRepository


def _environment(path: Path) -> dict[str, str]:
    supported = {
        "ANTHROPIC_API_KEY",
        "RECKONER_OWNER_DSN",
        "RECKONER_RUNNER_DSN",
        "RECKONER_EVALUATOR_DSN",
        "RECKONER_API_DSN",
    }
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ValueError("invalid environment") from error
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in supported:
            raise ValueError("invalid environment")
        values[key] = value
    return values


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reckoner")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--source-dir", type=Path, required=True)
    prepare_command.add_argument("--output", type=Path, required=True)
    verify_command = commands.add_parser("verify")
    verify_command.add_argument("--artifact-dir", type=Path, required=True)
    verify_command.add_argument("--source-dir", type=Path, required=True)
    migrate_command = commands.add_parser("migrate")
    migrate_command.add_argument("--env-file", type=Path, required=True)
    import_command = commands.add_parser("import")
    import_command.add_argument("--env-file", type=Path, required=True)
    import_command.add_argument("--artifact-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            index = prepare(args.source_dir, args.output)
            result = {"bundle_id": index["bundle_id"]}
        elif args.command == "verify":
            index = verify_bundle(args.artifact_dir, args.source_dir)
            result = {"bundle_id": index["bundle_id"]}
        elif args.command == "migrate":
            owner_dsn = _environment(args.env_file).get("RECKONER_OWNER_DSN")
            if not owner_dsn:
                raise ValueError("invalid environment")
            migrate(owner_dsn)
            result = {"status": "migrated"}
        else:
            owner_dsn = _environment(args.env_file).get("RECKONER_OWNER_DSN")
            if not owner_dsn:
                raise ValueError("invalid environment")
            with PostgresRepository(owner_dsn) as repository:
                repository.import_bundle(args.artifact_dir)
            index = json.loads((args.artifact_dir / "bundle.json").read_text(encoding="utf-8"))
            result = {"bundle_id": index["bundle_id"]}
    except psycopg.Error:
        print(f"{args.command} failed: database unavailable", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError) as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

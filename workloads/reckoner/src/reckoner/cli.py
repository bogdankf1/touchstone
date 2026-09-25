"""Command-line entry point for Reckoner batch operations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg
from jsonschema import ValidationError

from reckoner.baseline.config import load_config
from reckoner.baseline.provider import AnthropicProvider, ProviderError
from reckoner.baseline.runner import execute_run, preflight
from reckoner.contracts import content_id, validate_document
from reckoner.data.artifacts import sha256_file, verify_bundle
from reckoner.data.cohort import prepare
from reckoner.storage.budget import BudgetExceeded
from reckoner.storage.migrate import migrate
from reckoner.storage.postgres import PostgresRepository
from reckoner.telemetry.otlp import export_run, replay

ROOT = Path(__file__).resolve().parents[4]
SCHEMAS = ROOT / "contracts" / "schemas"


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
    for name in ("preflight", "run"):
        command = commands.add_parser(name)
        command.add_argument("--env-file", type=Path, required=True)
        command.add_argument("--artifact-dir", type=Path, required=True)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--purpose", choices=("pilot", "baseline"), required=True)
        command.add_argument("--run-id", required=True)
        if name == "run":
            command.add_argument("--allow-paid", action="store_true")
    export_command = commands.add_parser("export")
    export_command.add_argument("--env-file", type=Path, required=True)
    export_command.add_argument("--run-id", required=True)
    export_command.add_argument("--output", type=Path, required=True)
    replay_command = commands.add_parser("replay")
    replay_command.add_argument("--artifact-dir", type=Path, required=True)
    replay_command.add_argument("--endpoint", required=True)
    return parser


def _run_documents(path: Path) -> tuple[dict, dict, list[dict]]:
    config_path = path / "baseline-v1.json" if path.is_dir() else path
    directory = path if path.is_dir() else path.parent
    price_path = directory / "anthropic-prices-v1.json"
    config = load_config(config_path, price_path)
    try:
        price = json.loads(price_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid price document") from error
    thresholds = []
    wanted = dict(config["threshold_config_ids"])
    for candidate in sorted(directory.glob("thresholds-*.json")):
        try:
            document = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid threshold configuration") from error
        tenant_id = document.get("tenant_id") if isinstance(document, dict) else None
        if tenant_id in wanted and document.get("config_id") == wanted[tenant_id]:
            thresholds.append(document)
    if {document["tenant_id"] for document in thresholds} != set(wanted):
        raise ValueError("threshold configuration is unavailable")
    return config, price, thresholds


def _runtime_bundle_identity(artifact_dir: Path, purpose: str) -> str:
    """Validate only the runner-safe index, runtime cohort, and manifests."""
    try:
        index = json.loads((artifact_dir / "bundle.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid runtime bundle index") from error
    if not isinstance(index, dict):
        raise ValueError("invalid runtime bundle index")
    try:
        validate_document(index, SCHEMAS / "dataset-bundle-v1.schema.json")
    except ValidationError as error:
        raise ValueError("invalid runtime bundle index") from error
    body = {key: value for key, value in index.items() if key != "bundle_id"}
    if index["bundle_id"] != content_id(body):
        raise ValueError("runtime bundle identity does not match contents")
    cohort = index["cohorts"][purpose]
    for logical_name in (cohort["runtime_file"], *cohort["manifest_files"]):
        metadata = index["files"][logical_name]
        relative = Path(metadata["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("runtime artifact path escapes bundle")
        path = (artifact_dir / relative).resolve()
        if not path.is_relative_to(artifact_dir.resolve()):
            raise ValueError("runtime artifact path escapes bundle")
        if not path.is_file() or sha256_file(path) != metadata["sha256"]:
            raise ValueError(f"runtime artifact checksum mismatch: {relative}")
    return index["bundle_id"]


def _prepare_run(args, environment: dict[str, str]):
    runner_dsn = environment.get("RECKONER_RUNNER_DSN")
    api_key = environment.get("ANTHROPIC_API_KEY")
    if not runner_dsn or not api_key:
        raise ValueError("invalid environment")
    bundle_id = _runtime_bundle_identity(args.artifact_dir, args.purpose)
    config, price, thresholds = _run_documents(args.config)
    provider = AnthropicProvider(api_key)
    repository = PostgresRepository(runner_dsn)
    try:
        for document in thresholds:
            repository.register_threshold_config(document)
        repository.create_run(
            args.run_id,
            args.purpose,
            config,
            bundle_id,
            price=price,
            execution_mode="paid",
        )
    except BaseException:
        repository.close()
        raise
    return repository, provider


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
        elif args.command == "import":
            owner_dsn = _environment(args.env_file).get("RECKONER_OWNER_DSN")
            if not owner_dsn:
                raise ValueError("invalid environment")
            with PostgresRepository(owner_dsn) as repository:
                repository.import_bundle(args.artifact_dir)
            index = json.loads((args.artifact_dir / "bundle.json").read_text(encoding="utf-8"))
            result = {"bundle_id": index["bundle_id"]}
        elif args.command in {"preflight", "run"}:
            if args.command == "run" and not args.allow_paid:
                raise ValueError("paid execution requires --allow-paid")
            environment = _environment(args.env_file)
            repository, provider = _prepare_run(args, environment)
            with repository:
                if args.command == "preflight":
                    result = preflight(repository, provider, args.run_id)
                else:
                    result = execute_run(repository, provider, args.run_id)
                    if result["pending"] or result["uncertain"] or result["failed"]:
                        print(json.dumps(result, sort_keys=True))
                        return 3
        elif args.command == "export":
            runner_dsn = _environment(args.env_file).get("RECKONER_RUNNER_DSN")
            if not runner_dsn:
                raise ValueError("invalid environment")
            with PostgresRepository(runner_dsn) as repository:
                result = export_run(repository, args.run_id, args.output)
        else:
            result = replay(args.artifact_dir, args.endpoint)
            if result["pending"]:
                print(json.dumps(result, sort_keys=True))
                return 3
    except psycopg.Error:
        print(f"{args.command} failed: database unavailable", file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError) as error:
        print(f"{args.command} failed: {error}", file=sys.stderr)
        return 2
    except (BudgetExceeded, ProviderError):
        print(f"{args.command} blocked", file=sys.stderr)
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

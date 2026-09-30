"""No-provider preparation and owner-only import commands."""

import os
from pathlib import Path

from reckoner.v1.data.prepare import import_v1, prepare_v1


def register(commands):
    group = commands.add_parser("v1")
    subcommands = group.add_subparsers(dest="v1_command", required=True)
    preparation = subcommands.add_parser("prepare")
    preparation.add_argument("--source", type=Path, required=True)
    preparation.add_argument("--baseline-bundle", type=Path, required=True)
    preparation.add_argument("--output", type=Path, required=True)
    importer = subcommands.add_parser("import")
    importer.add_argument("--bundle", type=Path, required=True)
    importer.add_argument("--env-file", type=Path, required=True)


def execute(args):
    if args.v1_command == "prepare":
        return prepare_v1(args.source, args.baseline_bundle, args.output)
    supported = {"RECKONER_OWNER_DSN", "RECKONER_SOURCE_DIR", "RECKONER_BASELINE_BUNDLE"}
    if args.env_file == Path("-"):
        environment = {key: os.environ[key] for key in supported if key in os.environ}
    else:
        environment = {}
        for raw in args.env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or key not in supported:
                raise ValueError("v1 import requires an owner-only environment file")
            environment[key] = value
    if not environment.get("RECKONER_OWNER_DSN") or not environment.get("RECKONER_SOURCE_DIR"):
        raise ValueError("v1 import requires owner DSN and source directory")
    baseline = environment.get("RECKONER_BASELINE_BUNDLE")
    return import_v1(
        args.bundle,
        Path(environment["RECKONER_SOURCE_DIR"]),
        environment["RECKONER_OWNER_DSN"],
        Path(baseline) if baseline else None,
    )

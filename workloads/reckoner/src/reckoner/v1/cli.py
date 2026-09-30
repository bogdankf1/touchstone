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
    scorer = subcommands.add_parser("score")
    scorer.add_argument("--manifest", type=Path, required=True)
    scorer.add_argument("--protocol", type=Path, required=True)
    scorer.add_argument("--env-file", type=Path, required=True)


def execute(args):
    if args.v1_command == "score":
        return _score(args)
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


def _score(args):
    import json

    from reckoner.contracts import content_id
    from reckoner.v1.contracts import validate_v1
    from reckoner.v1.providers.jev import JevClient, build_request
    from reckoner.v1.storage.attempts import _preflight, score_task
    from reckoner.v1.storage.budget import ProviderBudget, validate_protocol
    from reckoner.v1.storage.repository import V1Repository

    manifest = validate_v1("experiment", json.loads(args.manifest.read_text()))
    protocol = validate_protocol(json.loads(args.protocol.read_text()))
    if (
        protocol["tenant_id"] != manifest["tenant_id"]
        or protocol["run_id"] != manifest["run_id"]
        or protocol["purpose"] != manifest["purpose"]
        or protocol["provider"] != "typesafe"
        or sorted((t["task_id"], t["transaction_id"]) for t in protocol["tasks"])
        != sorted((t["task_id"], t["transaction_id"]) for t in manifest["tasks"])
    ):
        raise ValueError("protocol exact cases do not match manifest")
    supported = {"RECKONER_RUNNER_DSN", "JEV_API_KEY"}
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
                raise ValueError("score requires a runner-only environment file")
            environment[key] = value
    if not all(environment.get(key) for key in supported):
        raise ValueError("score requires RECKONER_RUNNER_DSN and JEV_API_KEY")
    with V1Repository(environment["RECKONER_RUNNER_DSN"]) as repo:
        stored = repo._connection.execute(
            "SELECT document FROM reckoner.v1_runs WHERE tenant_id=%s AND run_id=%s",
            (manifest["tenant_id"], manifest["run_id"]),
        ).fetchone()
        if stored is None or stored["document"] != manifest:
            raise ValueError("manifest does not match immutable stored run")
        ledger = ProviderBudget(repo._connection)
        with repo._connection.transaction():
            from reckoner.storage.budget import ACCOUNTING_LOCK

            repo._connection.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
            ledger._check_legacy(repo._connection.cursor())
        prepared = []
        for case in protocol["tasks"]:
            task = repo.task(manifest["tenant_id"], manifest["run_id"], case["task_id"])
            rows = repo._connection.execute(
                "SELECT document FROM reckoner.v1_evidence "
                "WHERE tenant_id=%s AND transaction_id=%s ORDER BY evidence_id",
                (task["tenant_id"], task["transaction_id"]),
            ).fetchall()
            matches = [
                r["document"]
                for r in rows
                if content_id(build_request(task["transaction"], r["document"]))
                == case["request_sha256"]
            ]
            if len(matches) != 1:
                raise ValueError("protocol needs exactly one pinned prepared evidence record")
            _preflight(repo, task, matches[0], protocol)
            prepared.append((task, matches[0]))
        client = JevClient(environment["JEV_API_KEY"])
        statuses = []
        for task, evidence in prepared:
            result = score_task(repo, client, task, evidence, protocol)
            statuses.append(
                {
                    "task_id": task["task_id"],
                    "status": result.get("attempt_status", result.get("scorer_status")),
                }
            )
        return {"protocol_id": protocol["protocol_id"], "tasks": statuses}

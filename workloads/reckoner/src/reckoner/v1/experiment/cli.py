"""`reckoner v1 protocol ...`: measure, draft, validate, reserve, execute, verify.

Each step takes an explicit allowlisted environment file (never the root `.env`).
Drafting and validation are offline. Reservation needs the owner DSN and an
external approval record file; execution needs the runner DSN and the provider key
of the protocol's provider. Outputs are new files; nothing is overwritten.
"""

import json
import os
from pathlib import Path

ENVIRONMENTS = {
    "ledger": {"RECKONER_OWNER_DSN"},
    "measure": {"RECKONER_RUNNER_DSN"},
    "reserve": {"RECKONER_OWNER_DSN"},
    "execute": {"RECKONER_RUNNER_DSN", "JEV_API_KEY", "ANTHROPIC_API_KEY"},
    "verify": {"RECKONER_RUNNER_DSN"},
    "ledger-restore": {"RECKONER_OWNER_DSN", "RECKONER_LEDGER_STAGING_DSN"},
}


def register(subcommands):
    group = subcommands.add_parser("protocol")
    steps = group.add_subparsers(dest="protocol_step", required=True)
    ledger = steps.add_parser("ledger")
    ledger.add_argument("--provider", choices=("typesafe", "anthropic"), required=True)
    ledger.add_argument("--output", type=Path, required=True)
    measure = steps.add_parser("measure")
    measure.add_argument("--manifest", type=Path, action="append", required=True)
    measure.add_argument("--runs", type=Path, required=True)
    measure.add_argument("--output", type=Path, required=True)
    draft = steps.add_parser("draft")
    draft.add_argument("--purpose", required=True)
    draft.add_argument("--approver", required=True)
    draft.add_argument("--manifest", type=Path, action="append", required=True)
    draft.add_argument("--runs", type=Path, required=True)
    draft.add_argument("--measurements", type=Path, required=True)
    draft.add_argument("--prices", type=Path, required=True)
    draft.add_argument("--ledger", type=Path, required=True)
    draft.add_argument("--code-revision", required=True)
    draft.add_argument("--token-overhead", type=Path)
    draft.add_argument("--output", type=Path, required=True)
    validate = steps.add_parser("validate")
    validate.add_argument("--protocol", type=Path, required=True)
    validate.add_argument("--ledger", type=Path, required=True)
    reserve = steps.add_parser("reserve")
    reserve.add_argument("--protocol", type=Path, required=True)
    reserve.add_argument("--approval", type=Path, required=True)
    execute = steps.add_parser("execute")
    execute.add_argument("--protocol-sha256", required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--data-kind", choices=("simulated-cctd", "fabricated"))
    execute.add_argument("--calibration", type=Path)
    verify = steps.add_parser("verify")
    verify.add_argument("--protocol", type=Path, required=True)
    verify.add_argument("--report", type=Path, required=True)
    verify.add_argument("--execution-kind", choices=("fixture", "measured"), required=True)
    restore = steps.add_parser("ledger-restore")
    restore.add_argument("--dump", type=Path, required=True)
    restore.add_argument("--expected-sha256", required=True)
    restore.add_argument("--staging-container", required=True)
    for parser in (ledger, measure, reserve, execute, verify, restore):
        parser.add_argument("--env-file", required=True)


def environment(path, step):
    allowed = ENVIRONMENTS[step]
    if path == "-":
        return {key: os.environ[key] for key in allowed if key in os.environ}
    values = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in allowed:
            raise ValueError(f"protocol {step} accepts only {sorted(allowed)}")
        values[key] = value
    return values


def _need(values, *keys):
    if not all(values.get(key) for key in keys):
        raise ValueError("environment requires " + ", ".join(keys))
    return [values[key] for key in keys]


def _write_new(path: Path, text: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)


def _json(path):
    return json.loads(Path(path).read_text()) if path is not None else None


def _cases(manifests, runs):
    by_tx = {}
    for item in manifests:
        for case in item["manifest"]["cases"]:
            by_tx[(case["tenant_id"], case["transaction_id"])] = case
    cases = []
    for run in runs:
        for task in run["tasks"]:
            entry = by_tx.get((run["tenant_id"], task["transaction_id"]))
            if entry is None:
                raise ValueError("declared run task is not in the published manifests")
            cases.append(
                {
                    **task,
                    "tenant_id": run["tenant_id"],
                    "run_id": run["run_id"],
                    "evidence_id": entry["evidence_id"],
                }
            )
    return cases


def run(args):
    import psycopg

    from reckoner.v1.experiment import protocol as protocols
    from reckoner.v1.storage.budget import ProviderBudget
    from reckoner.v1.storage.repository import V1Repository

    step = args.protocol_step
    values = environment(args.env_file, step) if hasattr(args, "env_file") else {}
    if step == "ledger":
        (owner,) = _need(values, "RECKONER_OWNER_DSN")
        with psycopg.connect(owner, autocommit=True) as connection:
            snapshot = ProviderBudget(connection).snapshot(args.provider)
        _write_new(args.output, json.dumps(snapshot, indent=2, sort_keys=True))
        return snapshot
    if step == "measure":
        (runner,) = _need(values, "RECKONER_RUNNER_DSN")
        manifests = [protocols.load_evidence_manifest(path) for path in args.manifest]
        with V1Repository(runner) as repo:
            measured = protocols.measure_scoring_requests(repo, _cases(manifests, _json(args.runs)))
        _write_new(args.output, json.dumps(measured, indent=2, sort_keys=True))
        return {"measurement_id": measured["measurement_id"], **measured["distribution"]}
    if step == "draft":
        draft = protocols.draft_scoring_protocol(
            purpose=args.purpose,
            approver=args.approver,
            manifests=[protocols.load_evidence_manifest(path) for path in args.manifest],
            runs=_json(args.runs),
            measurements=_json(args.measurements),
            price_table=_json(args.prices),
            ledger=_json(args.ledger),
            code_revision=args.code_revision,
            token_overhead=_json(args.token_overhead),
        )
        _write_new(args.output, json.dumps(draft, indent=2, sort_keys=True))
        presentation = protocols.present_protocol(draft, _json(args.ledger))
        _write_new(Path(str(args.output) + ".md"), presentation)
        return {"protocol_sha256": draft["protocol_sha256"], "execution_authorized": False}
    if step == "validate":
        return protocols.validate_protocol(_json(args.protocol), _json(args.ledger))
    if step == "reserve":
        (owner,) = _need(values, "RECKONER_OWNER_DSN")
        with psycopg.connect(owner, autocommit=True) as connection:
            digest = protocols.reserve_protocol(
                _json(args.protocol), ProviderBudget(connection), approval=_json(args.approval)
            )
        return {"protocol_sha256": digest, "reserved": True}
    if step == "execute":
        from reckoner.v1.experiment.execute import execute_protocol

        (runner,) = _need(values, "RECKONER_RUNNER_DSN")
        clients = {}
        if values.get("JEV_API_KEY"):
            from reckoner.v1.providers.jev import JevClient

            clients["typesafe"] = JevClient(values["JEV_API_KEY"])
        if values.get("ANTHROPIC_API_KEY"):
            from reckoner.v1.notes.provider import NoteProvider

            clients["anthropic"] = NoteProvider(values["ANTHROPIC_API_KEY"])
        return execute_protocol(
            args.protocol_sha256,
            provider_clients=clients,
            dsn=runner,
            execution_kind="measured",
            output_dir=args.output,
            data_kind=args.data_kind,
            calibration=_json(args.calibration),
        )
    if step == "verify":
        from reckoner.v1.experiment.verify import collect_run_facts, verify_experiment

        (runner,) = _need(values, "RECKONER_RUNNER_DSN")
        protocol = _json(args.protocol)
        with V1Repository(runner) as repo:
            facts = collect_run_facts(repo, protocol, execution_kind=args.execution_kind)
        return verify_experiment(protocol, facts, _json(args.report))
    if step == "ledger-restore":
        from psycopg.conninfo import conninfo_to_dict

        from reckoner.v1.experiment.ledger import copy_legacy_ledger, restore_dump

        owner, staging = _need(values, "RECKONER_OWNER_DSN", "RECKONER_LEDGER_STAGING_DSN")
        restored = restore_dump(
            args.dump,
            expected_sha256=args.expected_sha256,
            staging_dsn=staging,
            command=[
                "docker",
                "exec",
                "-i",
                args.staging_container,
                "pg_restore",
                "-U",
                "postgres",
                "--no-owner",
                "--no-privileges",
                "-d",
                conninfo_to_dict(staging)["dbname"],
            ],
        )
        return copy_legacy_ledger(
            staging_dsn=staging, target_owner_dsn=owner, dump_sha256=restored["dump_sha256"]
        )
    raise ValueError("unknown protocol step")

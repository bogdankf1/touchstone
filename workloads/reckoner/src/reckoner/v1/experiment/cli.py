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
    "draft-notes": {"RECKONER_RUNNER_DSN"},
    "declare-run": {"RECKONER_OWNER_DSN"},
    "export-calibration": {"RECKONER_OWNER_DSN"},
    "register-calibration": {"RECKONER_OWNER_DSN"},
    "settle": {"RECKONER_OWNER_DSN"},
    "close": {"RECKONER_RUNNER_DSN"},
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
    measure.add_argument("--arms", type=Path)
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
    draft.add_argument("--arms", type=Path)
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
    close = steps.add_parser("close")
    close.add_argument("--protocol-sha256", required=True)
    verify = steps.add_parser("verify")
    verify.add_argument("--protocol-sha256", required=True)
    verify.add_argument("--report", type=Path, required=True)
    notes = steps.add_parser("draft-notes")
    notes.add_argument(
        "--purpose", choices=("note-judge-pilot", "final-notes", "final-judges"), required=True
    )
    notes.add_argument("--approver", required=True)
    notes.add_argument("--run", nargs=2, action="append", metavar=("TENANT_ID", "RUN_ID"),
                       required=True)  # fmt: skip
    notes.add_argument("--select", choices=("all-escalations", "hash-sample-v1"), required=True)
    notes.add_argument("--seed")
    notes.add_argument("--count", type=int)
    notes.add_argument("--prices", type=Path, required=True)
    notes.add_argument("--ledger", type=Path, required=True)
    notes.add_argument("--code-revision", required=True)
    notes.add_argument("--output", type=Path, required=True)
    declare = steps.add_parser("declare-run")
    declare.add_argument("--tenant-id", required=True)
    declare.add_argument("--run-id", required=True)
    declare.add_argument("--purpose", required=True)
    declare.add_argument("--tasks", type=Path, required=True)
    declare.add_argument("--dataset-version", required=True)
    declare.add_argument("--cohort-version", required=True)
    declare.add_argument("--code-revision", required=True)
    declare.add_argument("--created-at", required=True)
    declare.add_argument("--config-id")
    declare.add_argument(
        "--telemetry-mode", choices=("workflow", "scoring-only"), default="workflow"
    )
    export = steps.add_parser("export-calibration")
    export.add_argument("--protocol-sha256", required=True)
    export.add_argument("--bundle", type=Path, required=True)
    export.add_argument("--data-kind", choices=("simulated-cctd", "fabricated"), required=True)
    export.add_argument("--output", type=Path, required=True)
    register = steps.add_parser("register-calibration")
    register.add_argument("--report-dir", type=Path, required=True)
    register.add_argument("--protocol-sha256", required=True)
    register.add_argument("--development-protocol-sha256", required=True)
    register.add_argument("--bundle", type=Path, required=True)
    register.add_argument("--data-kind", choices=("simulated-cctd", "fabricated"), required=True)
    register.add_argument("--tenant-id", action="append", required=True)
    settle = steps.add_parser("settle")
    settle.add_argument("--call-id", required=True)
    settle.add_argument("--input-tokens", type=int, required=True)
    settle.add_argument("--output-tokens", type=int, required=True)
    settle.add_argument("--evidence", type=Path, required=True)
    restore = steps.add_parser("ledger-restore")
    restore.add_argument("--dump", type=Path, required=True)
    restore.add_argument("--expected-sha256", required=True)
    restore.add_argument("--staging-container", required=True)
    for parser in (
        ledger, measure, reserve, execute, close, verify, restore,
        notes, declare, export, register, settle,
    ):  # fmt: skip
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


def _arms(manifests, runs, arms):
    """Each run is pinned to exactly one evidence arm (required with several modes)."""
    modes = {item["manifest"]["mode"] for item in manifests}
    if arms is None:
        if len(modes) != 1:
            raise ValueError("--arms is required when manifests pin several evidence modes")
        return {(r["tenant_id"], r["run_id"]): next(iter(modes)) for r in runs}
    pinned = {(a["tenant_id"], a["run_id"]): a["evidence_mode"] for a in arms}
    if (
        set(pinned) != {(r["tenant_id"], r["run_id"]) for r in runs}
        or not set(pinned.values()) <= modes
    ):
        raise ValueError("every declared run needs exactly one pinned manifest arm")
    return pinned


def _cases(manifests, runs, arms=None):
    """Run tasks joined to their own arm's manifest; arms never overwrite each other."""
    by_case = {}
    for item in manifests:
        mode = item["manifest"]["mode"]
        for case in item["manifest"]["cases"]:
            key = (case["tenant_id"], case["transaction_id"], mode)
            if key in by_case:
                raise ValueError("two pinned manifests repeat one case in the same arm")
            by_case[key] = case
    pinned = _arms(manifests, runs, arms)
    cases = []
    for run in runs:
        mode = pinned[(run["tenant_id"], run["run_id"])]
        for task in run["tasks"]:
            entry = by_case.get((run["tenant_id"], task["transaction_id"], mode))
            if entry is None:
                raise ValueError("declared run task is not in its arm's published manifest")
            cases.append(
                {
                    **task,
                    "tenant_id": run["tenant_id"],
                    "run_id": run["run_id"],
                    "evidence_id": entry["evidence_id"],
                    "evidence_mode": mode,
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
            measured = protocols.measure_scoring_requests(
                repo, _cases(manifests, _json(args.runs), _json(args.arms))
            )
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
            arms=_json(args.arms),
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
    if step == "close":
        from reckoner.v1.experiment.execute import close_protocol

        (runner,) = _need(values, "RECKONER_RUNNER_DSN")
        return close_protocol(args.protocol_sha256, dsn=runner)
    if step == "verify":
        from reckoner.v1.experiment.execute import load_recorded
        from reckoner.v1.experiment.verify import collect_run_facts, verify_experiment

        (runner,) = _need(values, "RECKONER_RUNNER_DSN")
        with V1Repository(runner) as repo:
            # The recorded body (validated) and the persisted transport label are used;
            # no operator-supplied protocol file or execution flag is trusted.
            protocol = load_recorded(repo, args.protocol_sha256)
            facts = collect_run_facts(repo, protocol)
        return verify_experiment(protocol, facts, _json(args.report))
    if step == "draft-notes":
        (runner,) = _need(values, "RECKONER_RUNNER_DSN")
        with V1Repository(runner) as repo:
            runs = []
            for tenant_id, run_id in args.run:
                row = repo._connection.execute(
                    "SELECT document FROM reckoner.v1_runs WHERE tenant_id=%s AND run_id=%s",
                    (tenant_id, run_id),
                ).fetchone()
                if row is None:
                    raise ValueError("draft-notes needs declared, executed workflow runs")
                runs.append(row["document"])
            if len({r["config_id"] for r in runs}) != 1:
                raise ValueError("note protocols need one frozen configuration across runs")
            chosen = protocols.escalation_selection(
                repo, runs, method=args.select, seed=args.seed, count=args.count
            )
            measured = protocols.measure_note_requests(repo, chosen["keys"])
            config = repo.workflow_document(
                "v1_configs", {"tenant_id": runs[0]["tenant_id"], "config_id": runs[0]["config_id"]}
            )
        draft = protocols.draft_note_protocol(
            purpose=args.purpose,
            approver=args.approver,
            runs=runs,
            measurements=measured,
            config=config,
            price_table=_json(args.prices),
            ledger=_json(args.ledger),
            code_revision=args.code_revision,
            selection=chosen["selection"],
        )
        _write_new(args.output, json.dumps(draft, indent=2, sort_keys=True))
        _write_new(
            Path(str(args.output) + ".md"), protocols.present_protocol(draft, _json(args.ledger))
        )
        return {"protocol_sha256": draft["protocol_sha256"], "execution_authorized": False}
    if step == "declare-run":
        from reckoner.v1.experiment.runs import declare_run

        (owner,) = _need(values, "RECKONER_OWNER_DSN")
        with V1Repository(owner) as repo:
            declared = declare_run(
                repo,
                tenant_id=args.tenant_id,
                run_id=args.run_id,
                purpose=args.purpose,
                tasks=_json(args.tasks),
                dataset_version=args.dataset_version,
                cohort_version=args.cohort_version,
                code_revision=args.code_revision,
                created_at=args.created_at,
                config_id=args.config_id,
                telemetry_mode=args.telemetry_mode,
            )
        return {
            "run_id": declared["manifest"]["run_id"],
            "experiment_id": declared["manifest"]["experiment_id"],
            "config_id": declared["manifest"]["config_id"],
            "config_source": declared["config_source"],
            "activation_version": declared["activation_version"],
        }
    if step in {"export-calibration", "register-calibration"}:
        from reckoner.v1.experiment import calibration
        from reckoner.v1.experiment.execute import load_recorded

        (owner,) = _need(values, "RECKONER_OWNER_DSN")
        with V1Repository(owner) as repo:
            protocol = load_recorded(repo, args.protocol_sha256)
            if step == "register-calibration":
                if protocol["purpose"] != "validation":
                    raise ValueError("--protocol-sha256 must be a validation-purpose protocol")
                development = load_recorded(repo, args.development_protocol_sha256)
                if development["purpose"] != "development":
                    raise ValueError(
                        "--development-protocol-sha256 must be a development-purpose protocol"
                    )
                context = calibration.calibration_context(repo, protocol, args.data_kind)
                if calibration.calibration_context(repo, development, args.data_kind) != context:
                    raise ValueError("development and validation contexts differ")
                rows = {
                    name: calibration.export_calibration_rows(
                        repo,
                        frozen=calibration.load_frozen_sample(args.bundle, name),
                        protocol=recorded,
                        data_kind=args.data_kind,
                    )["rows"]
                    for name, recorded in (("development", development), ("validation", protocol))
                }
                identity = calibration.register_selected(
                    repo,
                    report_dir=args.report_dir,
                    tenants=args.tenant_id,
                    context=context,
                    development_rows=rows["development"],
                    validation_rows=rows["validation"],
                )
                return {"calibration_id": identity, "tenants": sorted(args.tenant_id)}
            frozen = calibration.load_frozen_sample(args.bundle, protocol["purpose"])
            exported = calibration.export_calibration_rows(
                repo, frozen=frozen, protocol=protocol, data_kind=args.data_kind
            )
        _write_new(args.output, json.dumps(exported["rows"], indent=2, sort_keys=True))
        _write_new(
            Path(str(args.output) + ".provenance.json"),
            json.dumps(exported["provenance"], indent=2, sort_keys=True),
        )
        return exported["provenance"]
    if step == "settle":
        from reckoner.v1.experiment.ledger import load_settlement_evidence, reconcile_call

        (owner,) = _need(values, "RECKONER_OWNER_DSN")
        with psycopg.connect(owner, autocommit=True) as connection:
            return reconcile_call(
                connection,
                args.call_id,
                {"input_tokens": args.input_tokens, "output_tokens": args.output_tokens},
                evidence=load_settlement_evidence(args.evidence),
            )
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

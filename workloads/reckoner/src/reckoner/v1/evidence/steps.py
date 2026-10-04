"""`reckoner v1 evidence declare|run|publish|drop-working-set|graph-check`.

Prepares relational and GDS-augmented evidence for the frozen simulated populations and
persists it in the instance's operational database. Credentials come only from an
allow-listed environment (`--env-file -` reads those names from the process environment).
No provider is called and no provider key is ever read.
"""

import argparse
import json
import os
from pathlib import Path
from time import perf_counter

from reckoner.contracts import content_id
from reckoner.data.artifacts import canonical_json
from reckoner.v1.evidence import preparation as prep

EVIDENCE_KEYS = frozenset(
    {
        "RECKONER_OWNER_DSN",
        "RECKONER_RUNNER_DSN",
        "RECKONER_SOURCE_DIR",
        "RECKONER_BASELINE_BUNDLE",
        "RECKONER_NEO4J_URI",
        "RECKONER_NEO4J_USER",
        "RECKONER_NEO4J_PASSWORD",
    }
)
LOCK_SPACE = 0x3B


def read_environment(env_file) -> dict:
    """Only allow-listed names; an environment file naming anything else is refused."""
    if str(env_file) == "-":
        return {key: os.environ[key] for key in sorted(EVIDENCE_KEYS) if key in os.environ}
    environment = {}
    for raw in Path(env_file).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in EVIDENCE_KEYS:
            raise ValueError("evidence environment file names a key outside the allowlist")
        environment[key] = value
    return environment


def _require(environment, *keys):
    missing = [key for key in keys if not environment.get(key)]
    if missing:
        raise ValueError("evidence preparation requires " + ", ".join(missing))
    return [environment[key] for key in keys]


def output_directory(root, declaration) -> Path:
    """`<root>/<prep12>/`; an existing directory is reused only for the same preparation."""
    path = Path(root) / declaration["preparation_id"][:12]
    payload = canonical_json(declaration)
    target = path / "declaration.json"
    if declaration["preparation_id"] != content_id(
        {k: v for k, v in declaration.items() if k != "preparation_id"}
    ):
        raise ValueError("declaration preparation identity mismatch")
    if target.exists():
        if target.read_bytes() != payload:
            raise ValueError("output directory belongs to another preparation")
        return path
    path.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(payload)
    return path


def _write_json_once(path: Path, value) -> None:
    prep.write_once(path, canonical_json(value))


def _append(path: Path, record: dict) -> None:
    with path.open("a") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _log(message: str) -> None:
    from reckoner.v1.evidence.rolling import progress

    progress(message)


def _populations(values) -> dict:
    if not values:
        return dict(prep.DEFAULT_POPULATIONS)
    populations = {}
    for item in values:
        name, separator, modes = item.partition("=")
        if not separator or not modes:
            raise ValueError("population must be NAME=MODE[,MODE]")
        populations[name] = tuple(modes.split(","))
    return populations


def _schedule_summary(schedule) -> list[dict]:
    return [
        {
            "day": entry["day"],
            "cases": [
                {k: c[k] for k in ("tenant_id", "transaction_id", "population", "modes", "pilot")}
                for c in entry["cases"]
            ],
        }
        for entry in schedule
    ]


# --- declare ----------------------------------------------------------------------


def _declare(args):
    from reckoner.v1.data.prepare import verify_preparation

    environment = read_environment(args.env_file)
    source, baseline = _require(environment, "RECKONER_SOURCE_DIR", "RECKONER_BASELINE_BUNDLE")
    bundle = Path(args.bundle)
    verify_preparation(bundle, Path(source))
    scaler = json.loads(Path(args.scaler).read_text())
    populations = _populations(args.population)
    schedule = prep.query_schedule(bundle, Path(baseline), populations)
    started = perf_counter()
    manifest = prep.entity_manifest(bundle, Path(source))
    manifest_seconds = perf_counter() - started
    declaration = prep.declare_preparation(
        bundle=bundle,
        baseline_bundle=Path(baseline),
        scaler=scaler,
        entity_manifest_id=manifest["entity_manifest_id"],
        schedule=schedule,
        populations=populations,
    )
    output = output_directory(args.output_root, declaration)
    _write_json_once(output / "entity-manifest.json", manifest)
    _write_json_once(output / "schedule.json", _schedule_summary(schedule))
    _write_json_once(
        output / "inputs.json",
        {
            "bundle": str(bundle.resolve()),
            "scaler": str(Path(args.scaler).resolve()),
            "populations": {k: list(v) for k, v in sorted(populations.items())},
        },
    )
    return {
        "preparation_id": declaration["preparation_id"],
        "source_snapshot_id": declaration["source_snapshot_id"],
        "entity_manifest_id": manifest["entity_manifest_id"],
        "entity_counts": manifest["counts"],
        "entity_manifest_seconds": manifest_seconds,
        "output": str(output),
        "populations": declaration["populations"],
    }


class Context:
    """Re-derives every declared identity before any store is touched."""

    def __init__(self, args):
        self.args = args
        self.declaration = json.loads(Path(args.declaration).read_text())
        self.output = Path(args.declaration).parent
        if output_directory(self.output.parent, self.declaration) != self.output:
            raise ValueError("declaration is not inside its preparation output directory")
        self.environment = read_environment(args.env_file)
        inputs = json.loads((self.output / "inputs.json").read_text())
        self.bundle, self.scaler_path = Path(inputs["bundle"]), Path(inputs["scaler"])
        self.populations = {k: tuple(v) for k, v in inputs["populations"].items()}
        self.scaler = json.loads(self.scaler_path.read_text())
        self.manifest = json.loads((self.output / "entity-manifest.json").read_text())
        if self.manifest["entity_manifest_id"] != content_id(
            {k: v for k, v in self.manifest.items() if k != "entity_manifest_id"}
        ):
            raise ValueError("entity manifest identity mismatch")

    def schedule(self, baseline):
        schedule = prep.query_schedule(self.bundle, Path(baseline), self.populations)
        again = prep.declare_preparation(
            bundle=self.bundle,
            baseline_bundle=Path(baseline),
            scaler=self.scaler,
            entity_manifest_id=self.manifest["entity_manifest_id"],
            schedule=schedule,
            populations=self.populations,
        )
        if again != self.declaration:
            raise ValueError("inputs no longer reproduce the declared preparation")
        return schedule

    @property
    def snapshot(self):
        return self.declaration["source_snapshot_id"]

    @property
    def config(self):
        return {"scaler_id": self.scaler["scaler_id"], "scaler": self.scaler}

    def identities(self):
        return [
            {
                "tenant_id": e["tenant_id"],
                "merchant_id": e["identity"],
                "identity": e["shared_identity"],
            }
            for e in self.manifest["entities"]
            if e["kind"] == "merchant"
        ]


class RunLock:
    """One runner per preparation: a session advisory lock in the operational database."""

    def __init__(self, owner_dsn, preparation_id):
        import psycopg

        self.connection = psycopg.connect(owner_dsn, autocommit=True)
        key = int(preparation_id[:14], 16)
        if not self.connection.execute(
            "SELECT pg_try_advisory_lock(%s, %s)", (LOCK_SPACE, key & 0x7FFFFFFF)
        ).fetchone()[0]:
            self.connection.close()
            raise RuntimeError("another evidence run holds this preparation's lock")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.connection.close()


def _guard(args, output):
    from reckoner.v1.evidence.rolling import StoreGuard

    return StoreGuard(
        free_path=args.free_path or output,
        free_floor_bytes=args.free_floor_bytes,
        max_store_bytes=args.max_store_bytes,
        vm_free_path=getattr(args, "vm_free_path", None),
        vm_free_floor_bytes=getattr(args, "vm_free_floor_bytes", 0),
    )


# --- relational pass ------------------------------------------------------------------


def _run_relational(context: Context, guard) -> dict:
    from reckoner.v1.data.history import SourceHistory
    from reckoner.v1.evidence.rolling import (
        RollingStore,
        SourceDays,
        ensure_working_set,
        facts,
        import_queries,
        persist_documents,
        persisted_summaries,
        with_database,
        working_set_name,
    )

    owner, runner, source, baseline = _require(
        context.environment,
        "RECKONER_OWNER_DSN",
        "RECKONER_RUNNER_DSN",
        "RECKONER_SOURCE_DIR",
        "RECKONER_BASELINE_BUNDLE",
    )
    declaration, through = context.declaration, context.args.through
    receipts_path = context.output / "days-relational.jsonl"
    with RunLock(owner, declaration["preparation_id"]):
        schedule = context.schedule(baseline)
        entries = {entry["day"]: entry for entry in schedule}
        ws_owner = ensure_working_set(owner, declaration["preparation_id"])
        ws_runner = with_database(runner, working_set_name(declaration["preparation_id"]))
        with (
            SourceHistory(context.bundle, Path(source)) as history,
            SourceDays(history, context.bundle) as days,
        ):
            store = RollingStore(
                ws_owner,
                ws_runner,
                source=days,
                scaler=context.scaler,
                snapshot_id=context.snapshot,
                guard=guard,
            )
            store.initialize(context.identities())
            identity = store.runtime_identity()
            known = facts(persisted_summaries(runner, context.snapshot))
            plan = prep.plan_run(
                schedule,
                known,
                receipts=_jsonl(receipts_path),
                mode="relational",
                through=through,
                snapshot=context.snapshot,
            )
            for day in plan["reconstruct"]:
                _append(
                    receipts_path, prep.reconstruct_receipt(entries[day], known, mode="relational")
                )
            for day in plan["pending"]:
                cases = [c for c in entries[day]["cases"] if "relational" in c["modes"]]
                transactions = [c["transaction"] for c in cases]
                _log(f"start relational {day} ({len(cases)} cases)")
                timings, began = {}, perf_counter()
                store.verify_queries(transactions)
                timings["verify_seconds"] = perf_counter() - began
                started = perf_counter()
                imported = store.advance_to(day, progress=_log)
                timings["advance_seconds"] = perf_counter() - started
                started = perf_counter()
                evicted = store.evict_before(prep.window(day)[0])
                timings["evict_seconds"] = perf_counter() - started
                started = perf_counter()
                # Includes previous-card rows outside the window, imported by evidence().
                documents = store.evidence(transactions, context.config)
                previous = store.previous
                timings["assembly_seconds"] = perf_counter() - started
                summary = prep.check_documents(
                    day, [(c, "relational", d) for c, d in zip(cases, documents, strict=True)]
                )
                started = perf_counter()
                import_queries(owner, transactions)
                ids = persist_documents(runner, documents)
                timings["persist_seconds"] = perf_counter() - started
                receipt = {
                    "day": day,
                    "pass": "relational",
                    "cases": len(cases),
                    "evidence_ids": sorted(ids),
                    "document_json_bytes": sum(len(json.dumps(d)) for d in documents),
                    "imported_days": len(imported),
                    "imported_rows": sum(i["rows"] for i in imported),
                    "imported_vectors": sum(i["vectors"] for i in imported),
                    "import_read_seconds": sum(i["read_seconds"] for i in imported),
                    "import_vector_seconds": sum(i["vector_seconds"] for i in imported),
                    "import_write_seconds": sum(i["write_seconds"] for i in imported),
                    "evicted_rows": evicted["rows"],
                    "evicted_vectors": evicted["vectors"],
                    "evict_delete_seconds": evicted["delete_seconds"],
                    "vacuum_seconds": evicted["vacuum_seconds"],
                    "previous_card_rows": previous["rows"],
                    "coverage": summary,
                    "guard": evicted["guard"],
                    "seconds": perf_counter() - began,
                    **timings,
                    "receipt_reconstructed": False,
                }
                _append(receipts_path, receipt)
                _log(
                    f"relational {day}: {len(cases)} cases, {receipt['imported_rows']} rows in, "
                    f"{receipt['evicted_rows']} out, {receipt['seconds']:.1f}s"
                )
    return {
        "preparation_id": declaration["preparation_id"],
        "through": through,
        "runtime_identity": identity,
        "complete_days": len(plan["complete"]),
        "processed_days": len(plan["pending"]),
        "reconstructed_receipts": len(plan["reconstruct"]),
        "stage": _stage(context, runner, "relational", through),
    }


# --- graph pass ---------------------------------------------------------------------


def _driver(environment):
    from neo4j import GraphDatabase

    uri, user, password = _require(
        environment, "RECKONER_NEO4J_URI", "RECKONER_NEO4J_USER", "RECKONER_NEO4J_PASSWORD"
    )
    driver = GraphDatabase.driver(uri, auth=(user, password), warn_notification_severity="OFF")
    driver.verify_connectivity()
    return driver


def _relational_manifests(context) -> list[dict]:
    manifests = []
    for name, modes in sorted(context.populations.items()):
        if "gds-augmented" not in modes:
            continue
        path = context.output / "manifests" / f"{name}-relational.json"
        if not path.exists():
            raise ValueError(f"publish the {name} relational manifest before the graph pass")
        manifests.append(json.loads(path.read_text()))
    return manifests


def _referenced(runner):
    import psycopg

    from reckoner.v1.evidence.rolling import runtime_dsn

    def referenced(projection_id):
        with psycopg.connect(runtime_dsn(runner)) as connection:
            return connection.execute(
                "SELECT EXISTS (SELECT 1 FROM reckoner.v1_evidence "
                "WHERE document->'source_snapshot_ids'->>'graph' = %s)",
                (projection_id,),
            ).fetchone()[0]

    return referenced


def _runner_identity(runner) -> dict:
    import psycopg

    from reckoner.v1.benchmark.steps import check_runtime_identity, runtime_identity
    from reckoner.v1.evidence.rolling import RUNTIME_ROLE, runtime_dsn

    with psycopg.connect(runtime_dsn(runner)) as connection:
        return check_runtime_identity(runtime_identity(connection), RUNTIME_ROLE)


def _stage(context, runner, mode, through) -> dict:
    """Per-stage receipt: coverage, missing reasons, non-convergence and snapshot ages."""
    from reckoner.v1.evidence.rolling import persisted_summaries

    summary = {
        "through": through,
        **prep.stage_summary(persisted_summaries(runner, context.snapshot), mode=mode),
    }
    _append(context.output / "stages.jsonl", summary)
    return summary


def _run_graph(context: Context, guard) -> dict:
    from reckoner.v1.data.history import SourceHistory
    from reckoner.v1.evidence import rolling
    from reckoner.v1.evidence.rolling import (
        PersistedRelational,
        SourceDays,
        facts,
        persist_documents,
        persisted_summaries,
    )
    from reckoner.v1.evidence.rolling_graph import RollingGraph, gds_documents

    owner, runner, source, baseline = _require(
        context.environment,
        "RECKONER_OWNER_DSN",
        "RECKONER_RUNNER_DSN",
        "RECKONER_SOURCE_DIR",
        "RECKONER_BASELINE_BUNDLE",
    )
    declaration, through = context.declaration, context.args.through
    receipts_path = context.output / "days-graph.jsonl"
    with RunLock(owner, declaration["preparation_id"]), _driver(context.environment) as driver:
        schedule = context.schedule(baseline)
        entries = {entry["day"]: entry for entry in schedule}
        manifests = _relational_manifests(context)
        PersistedRelational(runner, manifests)  # identity checks before any graph work
        neo4j = _require(
            context.environment,
            "RECKONER_NEO4J_URI",
            "RECKONER_NEO4J_USER",
            "RECKONER_NEO4J_PASSWORD",
        )
        graph = RollingGraph(
            driver,
            preparation_id=declaration["preparation_id"],
            snapshot_id=context.snapshot,
            guard=guard,
        )
        seeded = graph.seed(context.manifest)
        known = facts(persisted_summaries(runner, context.snapshot))
        plan = prep.plan_run(
            schedule,
            known,
            receipts=_jsonl(receipts_path),
            mode="gds-augmented",
            through=through,
            snapshot=context.snapshot,
        )
        identity = _runner_identity(runner)
        first_observed = prep.first_observations(context.manifest)
        projections_path = context.output / "projections.jsonl"
        logged = {r["cutoff"] for r in _jsonl(projections_path)}
        for day in plan["complete"]:
            # A completed day whose projection receipt was lost: the graph keeps receipts.
            if prep.iso(prep.day_start(day)) not in logged:
                kept = graph.receipt_for(day)
                if kept is None:
                    raise ValueError(f"no single kept projection receipt for completed {day}")
                _append(projections_path, {**kept, "receipt_reconstructed": True})
        for day in plan["reconstruct"]:
            _append(
                receipts_path, prep.reconstruct_receipt(entries[day], known, mode="gds-augmented")
            )
        with (
            SourceHistory(context.bundle, Path(source)) as history,
            SourceDays(history, context.bundle) as days,
        ):
            for day in plan["pending"]:
                cases = [c for c in entries[day]["cases"] if "gds-augmented" in c["modes"]]
                _log(f"start graph {day} ({len(cases)} cases)")
                began = perf_counter()
                imported = graph.advance_to(day, days, progress=_log)
                evicted = graph.evict_before(prep.window(day)[0])
                started = perf_counter()
                receipt = graph.projection_for(day, referenced=_referenced(runner))
                projection_seconds = perf_counter() - started
                started = perf_counter()
                documents = rolling.isolated(
                    gds_documents,
                    neo4j,
                    runner,
                    manifests,
                    [c["transaction"] for c in cases],
                    context.config,
                )
                assembly_seconds = perf_counter() - started
                summary = prep.check_documents(
                    day,
                    [(c, "gds-augmented", d) for c, d in zip(cases, documents, strict=True)],
                    first_observed=first_observed,
                )
                ids = persist_documents(runner, documents)
                # Record the receipt before its metric nodes go; receipts stay in the graph.
                _append(projections_path, {**receipt, "receipt_reconstructed": False})
                released = graph.release(receipt["projection_id"])
                record = {
                    "day": day,
                    "pass": "gds-augmented",
                    "cases": len(cases),
                    "evidence_ids": sorted(ids),
                    "imported_rows": sum(i["rows"] for i in imported),
                    "import_seconds": sum(i["seconds"] + i["read_seconds"] for i in imported),
                    "evicted_rows": evicted["rows"],
                    "evict_seconds": evicted["seconds"],
                    "projection_id": receipt["projection_id"],
                    "projection_action": receipt["action"],
                    "projection_seconds": projection_seconds,
                    "assembly_seconds": assembly_seconds,
                    "released_metrics": released,
                    "coverage": summary,
                    "seconds": perf_counter() - began,
                    "receipt_reconstructed": False,
                }
                _append(receipts_path, record)
                _log(f"graph {day}: {len(cases)} cases, {record['seconds']:.1f}s")
    stage = _stage(context, runner, "gds-augmented", through)
    return {
        "preparation_id": declaration["preparation_id"],
        "through": through,
        "runtime_identity": identity,
        "seed": seeded,
        "complete_days": len(plan["complete"]),
        "processed_days": len(plan["pending"]),
        "stage": stage,
    }


def _graph_check(context: Context, guard) -> dict:
    """Dedicated reconciliation day in a separate 3b-owned store (never the pass store)."""
    from reckoner.v1.data.history import SourceHistory
    from reckoner.v1.evidence.rolling import SourceDays
    from reckoner.v1.evidence.rolling_graph import RollingGraph, reference_summary

    (source,) = _require(context.environment, "RECKONER_SOURCE_DIR")
    day, cutoff = context.args.day, prep.iso(prep.day_start(context.args.day))
    with _driver(context.environment) as driver:
        graph = RollingGraph(
            driver,
            preparation_id=context.declaration["preparation_id"],
            snapshot_id=context.snapshot,
            purpose="graph-check",
            guard=guard,
        )
        started = perf_counter()
        seeded = graph.seed(context.manifest)
        with (
            SourceHistory(context.bundle, Path(source)) as history,
            SourceDays(history, context.bundle) as days,
        ):
            imported = graph.advance_to(day, days, progress=_log)
        import_seconds = perf_counter() - started
        receipt = graph.projection_for(day, referenced=lambda _: False)
        summary = graph.edge_summary(cutoff)
        with driver.session() as session:
            retention = session.run(
                "SHOW SETTINGS YIELD name, value WHERE name = "
                "'db.tx_log.rotation.retention_policy' RETURN value"
            ).single()
        result = {
            "schema_version": "reckoner-graph-check-v1",
            "dataset_simulated": True,
            "preparation_id": context.declaration["preparation_id"],
            "entity_manifest_id": context.manifest["entity_manifest_id"],
            "day": day,
            "seed": seeded,
            "imported_days": len(imported),
            "imported_rows": sum(i["rows"] for i in imported),
            "import_seconds": import_seconds,
            "projection": receipt,
            "edge_summary": summary,
            "tx_log_retention_policy": retention["value"] if retention else None,
        }
        if context.args.reference_records:
            rows = (
                json.loads(line)
                for line in Path(context.args.reference_records).open()
                if line.strip()
            )
            reference = reference_summary((row.get("transaction", row) for row in rows), cutoff)
            result["reference"] = reference
            result["reconciliation"] = {
                "card_merchant_equal": reference["card_merchant_id"] == summary["card_merchant_id"],
                "owns_delta": summary["owns"] - reference["owns"],
            }
        if context.args.reference_receipt:
            prior = json.loads(Path(context.args.reference_receipt).read_text())["projection"]
            reconciliation = result.setdefault("reconciliation", {})
            reconciliation["node_count_equal"] = prior["node_count"] == receipt["node_count"]
            reconciliation["prior_node_count"] = prior["node_count"]
            reconciliation["prior_edge_count"] = prior["edge_count"]
            if "reference" in result:
                prior_shared = (
                    prior["edge_count"] // 2
                    - result["reference"]["owns"]
                    - result["reference"]["card_merchant_edges"]
                )
                reconciliation["prior_shared_identity_implied"] = prior_shared
                reconciliation["shared_identity_delta"] = summary["shared_identity"] - prior_shared
        result["released_metrics"] = graph.release(receipt["projection_id"])
    _write_json_once(context.output / f"graph-check-{day}.json", result)
    return result


# --- publish and cleanup -------------------------------------------------------------


def _publish(context: Context) -> dict:
    from reckoner.v1.evidence.rolling import persisted_summaries

    runner, baseline = _require(
        context.environment, "RECKONER_RUNNER_DSN", "RECKONER_BASELINE_BUNDLE"
    )
    mode = {"relational": "relational", "graph": "gds-augmented"}[context.args.evidence_pass]
    schedule = context.schedule(baseline)
    summaries = persisted_summaries(runner, context.snapshot)
    written = prep.publish(
        context.declaration,
        schedule,
        summaries,
        context.output,
        mode=mode,
        populations=context.args.population or None,
    )
    return {
        "manifests": written,
        "persisted_document_bytes": sum(s["bytes"] for s in summaries),
    }


def _drop(context: Context) -> dict:
    from reckoner.v1.evidence.rolling import drop_working_set

    (owner,) = _require(context.environment, "RECKONER_OWNER_DSN")
    # Never while a run of this preparation holds the working set.
    with RunLock(owner, context.declaration["preparation_id"]):
        return drop_working_set(owner, context.declaration["preparation_id"])


def run(args):
    try:
        return _dispatch(args)
    except BaseException as error:
        _log(f"evidence {args.evidence_step} failed: {type(error).__name__}: {error}")
        raise


def _dispatch(args):
    if args.evidence_step == "declare":
        return _declare(args)
    context = Context(args)
    if args.evidence_step == "publish":
        return _publish(context)
    if args.evidence_step == "drop-working-set":
        return _drop(context)
    guard = _guard(args, context.output)
    if args.evidence_step == "graph-check":
        return _graph_check(context, guard)
    if args.evidence_pass == "relational":
        return _run_relational(context, guard)
    return _run_graph(context, guard)


def register(subcommands):
    from reckoner.v1.benchmark.resources import FREE_FLOOR

    group = subcommands.add_parser("evidence")
    steps = group.add_subparsers(dest="evidence_step", required=True)

    def floor(value):
        """The CLI may only raise the 15 GiB hard free-disk floor, never lower it."""
        number = int(value)
        if number < FREE_FLOOR:
            raise argparse.ArgumentTypeError(f"free-disk floor cannot go below {FREE_FLOOR}")
        return number

    def guarded(parser):
        parser.add_argument("--free-floor-bytes", type=floor, default=FREE_FLOOR)
        # The job container's own filesystem is the Docker Desktop VM disk that holds the
        # store volumes; it gets its own floor (measured: 474 GB sparse ext4, host-backed).
        parser.add_argument("--vm-free-path", type=Path, default=Path("/"))
        parser.add_argument("--vm-free-floor-bytes", type=floor, default=FREE_FLOOR)
        parser.add_argument("--max-store-bytes", type=int)
        parser.add_argument("--free-path", type=Path)

    declare = steps.add_parser("declare")
    declare.add_argument("--bundle", type=Path, required=True)
    declare.add_argument("--scaler", type=Path, required=True)
    declare.add_argument("--output-root", type=Path, required=True)
    declare.add_argument("--population", action="append", metavar="NAME=MODE[,MODE]")
    declare.add_argument("--env-file", required=True)
    runner = steps.add_parser("run")
    runner.add_argument("--declaration", type=Path, required=True)
    runner.add_argument(
        "--pass", dest="evidence_pass", choices=("relational", "graph"), required=True
    )
    runner.add_argument("--through", required=True, metavar="YYYY-MM-DD")
    runner.add_argument("--env-file", required=True)
    guarded(runner)
    publisher = steps.add_parser("publish")
    publisher.add_argument("--declaration", type=Path, required=True)
    publisher.add_argument(
        "--pass", dest="evidence_pass", choices=("relational", "graph"), required=True
    )
    publisher.add_argument(
        "--population",
        action="append",
        help="publish only these complete populations (the pilot follows development)",
    )
    publisher.add_argument("--env-file", required=True)
    drop = steps.add_parser("drop-working-set")
    drop.add_argument("--declaration", type=Path, required=True)
    drop.add_argument("--env-file", required=True)
    check = steps.add_parser("graph-check")
    check.add_argument("--declaration", type=Path, required=True)
    check.add_argument("--day", required=True, metavar="YYYY-MM-DD")
    check.add_argument("--reference-records", type=Path)
    check.add_argument("--reference-receipt", type=Path)
    check.add_argument("--env-file", required=True)
    guarded(check)

"""Store-step orchestration and the `reckoner v1 benchmark` CLI.

`reckoner v1 benchmark STEP`, run in this order against simulated-data stores:

- ``inventory``: evaluator-side preparation. The ONLY step that reads privileged
  oracle resolutions, to freeze each query's eligible candidate population.
- ``prepare``: candidate vectors from the full source history; no oracle reads.
- ``verify``: stored vectors equal the frozen union; strict coverage per query.
- ``measure``: runtime-role SQL/Cypher schedule and exact top five; no labels.
- ``replacement``: one first-pass-after-restart receipt checked against ``measure``.
- ``audit``: disk reconciliation of artifacts plus every mounted store volume.
- ``report``: offline evaluator assembly; joins query labels after retrieval.

Database URLs and credentials come only from environment variables NAMED by the
caller. No environment file is read and no provider is called.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from time import perf_counter

from reckoner.contracts import content_id
from reckoner.v1.benchmark import queries
from reckoner.v1.benchmark.assembly import artifact_record, assemble_report
from reckoner.v1.benchmark.protocol import (
    INVENTORY_SQL,
    check_protocol,
    check_stored,
    frozen_queries,
    inventory_parameters,
    inventory_population,
    measure_observation,
    protocol_declaration,
    replacement_receipt,
)
from reckoner.v1.benchmark.report import write_report
from reckoner.v1.benchmark.resources import (
    DERIVED_CAP,
    FREE_FLOOR,
    Guard,
    Sampler,
    disk_reconciliation,
    parse_stores,
    tree_bytes,
    volume_bytes,
)


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"environment variable {name} is not set")
    return value


def _json(path: Path):
    return json.loads(Path(path).read_text())


def _write_new(path: Path, value) -> None:
    with Path(path).open("x") as handle:
        handle.write(json.dumps(value, indent=2) + "\n")


def _require_absent(paths) -> None:
    """Pre-flight: every output of a step must be new, checked before any store work."""
    existing = [Path(p).name for p in paths if Path(p).exists()]
    if existing:
        raise FileExistsError("refusing to overwrite existing evidence: " + ", ".join(existing))


def _scaler(args, protocol=None) -> dict:
    scaler = _json(args.scaler)
    if protocol is not None and scaler["scaler_id"] != protocol["scaler_id"]:
        raise ValueError("scaler differs from the frozen protocol")
    return scaler


def _credentials(args):
    return _env(args.pg_dsn_env), (_env(args.neo4j_user_env), _env(args.neo4j_password_env))


def runtime_conninfo(dsn: str, role: str) -> str:
    """Apply the runtime role through libpq options, keeping any caller options."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    if not re.fullmatch(r"[a-z_][a-z0-9_]*", role):
        raise ValueError("invalid runtime role name")
    options = conninfo_to_dict(dsn).get("options")
    role_option = f"-c role={role}"
    return make_conninfo(dsn, options=f"{options} {role_option}" if options else role_option)


RUNTIME_IDENTITY_SQL = (
    "SELECT current_user, session_user, coalesce((SELECT has_schema_privilege("
    "current_user, n.oid, 'USAGE') FROM pg_namespace n WHERE n.nspname = 'oracle'), false)"
)


NEO4J_USER_CYPHER = "SHOW CURRENT USER YIELD user AS username"


def runtime_identity(connection) -> dict:
    current, session, oracle = connection.execute(RUNTIME_IDENTITY_SQL).fetchone()
    return {"current_user": current, "session_user": session, "oracle_usage": oracle}


def check_runtime_identity(identity: dict, role: str) -> dict:
    if identity["oracle_usage"]:
        raise ValueError("runtime session can use schema oracle; refusing to measure")
    if identity["current_user"] != role:
        raise ValueError(f"runtime role {role} is not the active session role")
    return identity


def _stores(args, scaler, credentials):
    from neo4j import GraphDatabase

    dsn, auth = credentials
    driver = GraphDatabase.driver(args.neo4j_uri, auth=auth, warn_notification_severity="OFF")
    sql = None
    try:
        driver.verify_connectivity()
        sql = queries.SQLQueries(runtime_conninfo(dsn, args.runtime_role), scaler)
        identity = check_runtime_identity(runtime_identity(sql.connection), args.runtime_role)
        with driver.session() as session:
            # Community edition has one database user; record the server-reported name.
            identity["neo4j_user"] = session.run(NEO4J_USER_CYPHER).single()["username"]
        graph = queries.CypherQueries(driver)
    except BaseException:
        if sql is not None:
            sql.close()
        driver.close()
        raise
    return driver, sql, graph, identity


def _inventory(args):
    import psycopg

    dsn = _env(args.pg_dsn_env)
    output = Path(args.output_dir)
    _require_absent(
        [output / n for n in ("candidates.jsonl", "protocol.json", "sql-function-plan.json")]
    )
    queries_ = frozen_queries(
        (Path(args.bundle) / "runtime_validation.jsonl").read_text().splitlines(), args.query_date
    )
    scaler, guard = _scaler(args), Guard(args)
    guard()  # Refuse before contacting the store or writing large outputs.
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("SET default_transaction_read_only=on")
        candidate_ids, members = inventory_population(
            {
                q["transaction_id"]: [
                    r[0] for r in connection.execute(INVENTORY_SQL, inventory_parameters(q))
                ]
                for q in queries_
            }
        )
        with (output / "candidates.jsonl").open("x") as handle:
            for tx in sorted(
                members.values(),
                key=lambda tx: (
                    tx["tenant_id"],
                    tx["card_id"],
                    tx["occurred_at"],
                    tx["transaction_id"],
                ),
            ):
                handle.write(json.dumps(tx, sort_keys=True) + "\n")
        protocol = protocol_declaration(
            queries_, candidate_ids, scaler["scaler_id"], guard(connection)
        )
        _write_new(output / "protocol.json", protocol)
        plan = connection.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM reckoner.v1_neighbours(%s,%s)",
            (queries_[0]["tenant_id"], queries_[0]["transaction_id"]),
        ).fetchone()[0]
        _write_new(output / "sql-function-plan.json", plan)
    return {
        k: protocol[k] for k in ("candidate_union_count", "candidate_union_hash", "protocol_id")
    }


VECTOR_COPY = (
    "COPY reckoner.v1_vectors (tenant_id, transaction_id, scaler_id, feature_version, "
    "features) FROM STDIN"
)


def write_vectors(pairs, handle, connection, *, scaler_id, batch_size, guard, guard_every):
    """One transaction: a failure stores no vectors. There is no resume.

    The resource guard (which may size volumes) runs at the start, every `guard_every`
    batches and at the end, not on every batch.
    """
    samples, batch, batches, count = [guard(connection)], [], 0, 0

    def flush():
        with connection.cursor() as cursor, cursor.copy(VECTOR_COPY) as copy:
            for row in batch:
                copy.write_row(row)
        batch.clear()

    for tx, vector in pairs:
        handle.write(
            json.dumps(
                {
                    "tenant_id": tx["tenant_id"],
                    "transaction_id": tx["transaction_id"],
                    "features": vector,
                },
                sort_keys=True,
            )
            + "\n"
        )
        batch.append((tx["tenant_id"], tx["transaction_id"], scaler_id, "features-v1", str(vector)))
        count += 1
        if len(batch) >= batch_size:
            flush()
            handle.flush()
            batches += 1
            if batches % guard_every == 0:
                samples.append(guard(connection))
    if batch:
        flush()
    samples.append(guard(connection))
    connection.commit()
    return count, samples


def _prepare(args):
    import psycopg

    from reckoner.v1.benchmark.queries import candidate_vectors
    from reckoner.v1.data.history import SourceHistory

    dsn = _env(args.pg_dsn_env)
    output = Path(args.output_dir)
    _require_absent([output / "vectors.jsonl", output / "preparation.json"])
    protocol = check_protocol(_json(output / "protocol.json"))
    scaler, guard = _scaler(args, protocol), Guard(args)
    candidates = [
        json.loads(line) for line in (output / "candidates.jsonl").read_text().splitlines()
    ]
    ids = sorted(tx["transaction_id"] for tx in candidates)
    if len(set(ids)) != len(ids):
        raise ValueError("candidate transaction_id appears twice or under two tenants")
    if (
        len(ids) != protocol["candidate_union_count"]
        or content_id(ids) != protocol["candidate_union_hash"]
    ):
        raise ValueError("candidate file differs from the frozen protocol union")
    guard()  # Refuse before reading source history or opening a write transaction.
    started = perf_counter()
    with (
        SourceHistory(args.bundle, args.source_dir) as history,
        psycopg.connect(dsn) as connection,
        (output / "vectors.jsonl").open("x") as handle,
    ):
        count, samples = write_vectors(
            candidate_vectors(candidates, history, scaler),
            handle,
            connection,
            scaler_id=scaler["scaler_id"],
            batch_size=args.batch_size,
            guard=guard,
            guard_every=args.guard_every,
        )
    result = {
        "count": count,
        "seconds": perf_counter() - started,
        "resources": samples,
        "scaler_id": scaler["scaler_id"],
    }
    _write_new(output / "preparation.json", result)
    return {"count": count, "seconds": result["seconds"]}


def _verify(args):
    import psycopg

    dsn = _env(args.pg_dsn_env)
    output = Path(args.output_dir)
    _require_absent([output / "stored-verification.json"])
    protocol = check_protocol(_json(output / "protocol.json"))
    scaler, guard = _scaler(args, protocol), Guard(args)
    guard()
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("SET default_transaction_read_only=on")
        stored = [
            r[0]
            for r in connection.execute(
                "SELECT transaction_id FROM reckoner.v1_vectors WHERE scaler_id=%s",
                (scaler["scaler_id"],),
            )
        ]
        coverage = [
            {
                "query_id": q["transaction_id"],
                "complete": connection.execute(
                    "SELECT reckoner.v1_vector_coverage(%s,%s,%s)",
                    (q["tenant_id"], q["transaction_id"], scaler["scaler_id"]),
                ).fetchone()[0],
            }
            for q in protocol["queries"]
        ]
        resources = guard(connection)
    result = {**check_stored(stored, coverage, protocol), "resources": resources}
    _write_new(output / "stored-verification.json", result)
    return {k: result[k] for k in ("stored_count", "stored_hash")}


def _measure(args):
    credentials = _credentials(args)
    output = Path(args.output_dir)
    names = ("service-samples.jsonl", "exact-memberships.json")
    _require_absent([*(output / n for n in names), *_report_pair(output / "retrieval-report")])
    protocol = check_protocol(_json(output / "protocol.json"))
    scaler, guard = _scaler(args, protocol), Guard(args)
    guard()  # No database query here: the first timed pass must follow the restart.
    driver, sql, graph, identity = _stores(args, scaler, credentials)
    sampler = Sampler(
        output / "service-samples.jsonl", args.sample_container, args.interval, raise_errors=False
    )
    try:
        with sampler:
            observation, memberships = measure_observation(
                protocol,
                sql,
                graph,
                exclusions=lambda q: sql.connection.execute(
                    "SELECT reckoner.v1_comparable_exclusions(%s,%s)",
                    (q["tenant_id"], q["transaction_id"]),
                ).fetchone()[0],
                resources=lambda: guard(sql.connection),
                first_pass_state=args.first_pass_state,
                runtime=identity,
            )
    finally:
        sql.close()
        graph.close()
        driver.close()
    # A finished one-shot pass is never discarded; incomplete sampling is recorded, then raised.
    observation["resource_samples_complete"] = sampler.complete
    with (output / "exact-memberships.json").open("x") as handle:
        json.dump(memberships, handle)
    report = write_report(observation, output / "retrieval-report")
    sampler.raise_error()
    return {"report_id": report["report_id"], "latency_ms": report["latency_ms"]}


def _report_pair(prefix: Path):
    return [prefix.with_name(prefix.name + ".json"), prefix.with_name(prefix.name + ".md")]


def _replacement(args):
    credentials = _credentials(args)
    output = Path(args.output_dir)
    _require_absent(
        [output / "replacement-service-samples.jsonl", output / "replacement-first-pass.json"]
    )
    protocol = check_protocol(_json(output / "protocol.json"))
    observation = _json(output / "retrieval-report.json")
    scaler, guard = _scaler(args, protocol), Guard(args)
    guard()
    driver, sql, graph, identity = _stores(args, scaler, credentials)
    sampler = Sampler(
        output / "replacement-service-samples.jsonl",
        args.sample_container,
        args.interval,
        raise_errors=False,
    )
    try:
        with sampler:
            receipt = replacement_receipt(
                protocol,
                observation,
                sql,
                graph,
                reason=args.reason,
                runtime=identity,
                resources=lambda: guard(sql.connection),
            )
    finally:
        sql.close()
        graph.close()
        driver.close()
    # Sampling completeness is known only after the sampler stops; reseal the identity.
    receipt = {k: v for k, v in receipt.items() if k != "receipt_id"}
    receipt["resource_samples_complete"] = sampler.complete
    receipt["receipt_id"] = content_id(receipt)
    _write_new(output / "replacement-first-pass.json", receipt)
    sampler.raise_error()
    return {"receipt_id": receipt["receipt_id"]}


def _docker(*command) -> str:
    return subprocess.check_output(["docker", *command], text=True)


def _audit(args):
    output = Path(args.output_dir)
    _require_absent(
        [output / f"resource-reconciliation-{args.label}.json"]
        + ([output / f"cgroup-{args.label}.json"] if args.cgroup_container else [])
    )
    names = _docker(
        "ps", "-a", "--filter", "name=" + args.container_prefix, "--format", "{{.Names}}"
    ).splitlines()
    inspected = json.loads(_docker("inspect", *names)) if names else []
    volumes = sorted({m["Name"] for c in inspected for m in c["Mounts"] if m["Type"] == "volume"})
    measured = volume_bytes({v: v for v in volumes}, args.du_image) if volumes else {}
    # Declared sizes (e.g. preserved stores and images measured once at stage start) count
    # toward the derived total; they are recorded separately from measured volumes.
    stores = parse_stores(args.store_bytes, measured)
    declared = {n: s["bytes"] for n, s in stores.items() if s["source"] == "declared"}
    result = disk_reconciliation(
        artifact_bytes=tree_bytes(args.artifact_root) + sum(declared.values()),
        volume_bytes=measured,
        free_bytes=shutil.disk_usage(output).free,
        containers=[
            {
                "name": c["Name"],
                "state": c["State"]["Status"],
                "oom_killed": c["State"]["OOMKilled"],
                "restart_count": c["RestartCount"],
            }
            for c in inspected
        ],
        free_floor=args.free_floor_bytes,
        derived_cap=args.derived_cap_bytes,
    )
    result["artifact_bytes"] -= sum(declared.values())
    result["declared_store_bytes"] = declared
    _write_new(output / f"resource-reconciliation-{args.label}.json", result)
    if args.cgroup_container:
        counters = {}
        for item in args.cgroup_container:
            name, _, container = item.partition("=")
            counters[name] = {
                key: _docker("exec", container, "cat", "/sys/fs/cgroup/" + key).strip()
                for key in ("memory.current", "memory.peak", "memory.events")
            }
        _write_new(output / f"cgroup-{args.label}.json", counters)
    return {
        "total_derived_bytes": result["total_derived_bytes"],
        "free_bytes": result["free_bytes"],
    }


def _report(args):
    labels = {
        row["transaction_id"]: row["label"]
        for row in (
            json.loads(line) for line in Path(args.oracle_labels).read_text().splitlines() if line
        )
    }
    inputs = [args.protocol, args.observation]
    inputs += [p for p in (args.replacement, args.resources, args.cgroup) if p is not None]
    inputs += [*args.samples, *args.attach]
    artifacts = {}
    for path in map(Path, inputs):
        if path.name in artifacts:
            raise ValueError(f"duplicate artifact name {path.name}")
        artifacts[path.name] = artifact_record(path)
    body = assemble_report(
        _json(args.observation),
        _json(args.protocol),
        labels=labels,
        annotations=_json(args.annotations),
        artifacts=artifacts,
        replacement=_json(args.replacement) if args.replacement else None,
        resources=_json(args.resources) if args.resources else None,
        cgroup=_json(args.cgroup) if args.cgroup else None,
        samples={
            Path(p).name: [json.loads(line) for line in Path(p).read_text().splitlines() if line]
            for p in args.samples
        },
    )
    report = write_report(body, args.output)
    return {"report_id": report["report_id"], "output": str(args.output)}


STEPS = {
    "inventory": _inventory,
    "prepare": _prepare,
    "verify": _verify,
    "measure": _measure,
    "replacement": _replacement,
    "audit": _audit,
    "report": _report,
}


def run(args):
    return STEPS[args.benchmark_step](args)


def register(subcommands):
    benchmark = subcommands.add_parser("benchmark")
    steps = benchmark.add_subparsers(dest="benchmark_step", required=True)

    def limits(parser):
        parser.add_argument("--free-floor-bytes", type=int, default=FREE_FLOOR)
        parser.add_argument("--derived-cap-bytes", type=int, default=DERIVED_CAP)

    def guarded(parser, scaler=True):
        parser.add_argument("--output-dir", type=Path, required=True)
        parser.add_argument("--artifact-root", type=Path, required=True)
        parser.add_argument("--store-bytes", action="append", default=[], metavar="NAME=BYTES")
        parser.add_argument("--store-volume", action="append", default=[], metavar="NAME=VOLUME")
        parser.add_argument("--du-image")
        parser.add_argument("--pg-dsn-env", required=True, metavar="ENV_VAR_NAME")
        if scaler:
            parser.add_argument("--scaler", type=Path, required=True)
        limits(parser)

    def graph(parser):
        parser.add_argument("--neo4j-uri", required=True)
        parser.add_argument("--neo4j-user-env", required=True, metavar="ENV_VAR_NAME")
        parser.add_argument("--neo4j-password-env", required=True, metavar="ENV_VAR_NAME")
        parser.add_argument("--runtime-role", default="reckoner_runner")
        parser.add_argument("--sample-container", action="append", default=[])
        parser.add_argument("--interval", type=float, default=5.0)

    inventory = steps.add_parser("inventory")
    guarded(inventory)
    inventory.add_argument("--bundle", type=Path, required=True)
    inventory.add_argument("--query-date", required=True)
    prepare = steps.add_parser("prepare")
    guarded(prepare)
    prepare.add_argument("--bundle", type=Path, required=True)
    prepare.add_argument("--source-dir", type=Path, required=True)
    prepare.add_argument("--batch-size", type=int, default=5000)
    prepare.add_argument("--guard-every", type=int, default=10, metavar="BATCHES")
    guarded(steps.add_parser("verify"))
    measure = steps.add_parser("measure")
    guarded(measure)
    graph(measure)
    measure.add_argument("--first-pass-state", required=True)
    replacement = steps.add_parser("replacement")
    guarded(replacement)
    replacement.add_argument("--reason", required=True)
    graph(replacement)
    audit = steps.add_parser("audit")
    audit.add_argument("--output-dir", type=Path, required=True)
    audit.add_argument("--artifact-root", type=Path, required=True)
    audit.add_argument("--container-prefix", required=True)
    audit.add_argument("--du-image", required=True)
    audit.add_argument("--label", required=True)
    audit.add_argument("--cgroup-container", action="append", default=[], metavar="NAME=CONTAINER")
    audit.add_argument("--store-bytes", action="append", default=[], metavar="NAME=BYTES")
    limits(audit)
    report = steps.add_parser("report")
    for name in ("--protocol", "--observation", "--oracle-labels", "--annotations", "--output"):
        report.add_argument(name, type=Path, required=True)
    for name in ("--replacement", "--resources", "--cgroup"):
        report.add_argument(name, type=Path)
    report.add_argument("--samples", type=Path, action="append", default=[])
    report.add_argument("--attach", type=Path, action="append", default=[])

"""Tracked retrieval benchmark harness: explicit inputs, guarded resources, auditable outputs.

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
import shutil
import subprocess
import threading
import time
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from time import perf_counter

from reckoner.contracts import content_id
from reckoner.v1.benchmark.queries import benchmark_queries, distribution
from reckoner.v1.benchmark.report import MEASUREMENT_MODES, RETRIEVAL_SCHEMA, write_report

GIB = 1024**3
FREE_FLOOR = 15 * GIB
DERIVED_CAP = 20 * GIB
PROTOCOL_SCHEMA = "retrieval-protocol-v1"
REPLACEMENT_SCHEMA = "retrieval-replacement-first-pass-v1"
ANNOTATION_SCHEMA = "retrieval-report-annotations-v1"
RECONCILIATION_SCHEMA = "phase3-disk-reconciliation-v1"
ANNOTATION_KEYS = {
    "schema_version",
    "limitations",
    "relevance_interpretation",
    "plan_observation",
    "resource_accounting_stage",
    "first_pass_protocol",
}
# Privileged evaluator read, used only by `inventory`.
INVENTORY_SQL = (
    "SELECT h.document FROM oracle.v1_resolutions r JOIN reckoner.v1_history h "
    "USING(tenant_id,transaction_id) WHERE r.tenant_id=%s "
    "AND r.resolution_policy_version='simulated-seven-days-v1' "
    "AND r.resolved_at<%s::timestamptz AND r.resolved_at>=%s::timestamptz-interval '90 days' "
    "AND h.occurred_at<%s::timestamptz AND (h.document->>'amount_minor')::bigint>0 "
    "ORDER BY h.transaction_id"
)
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}


class ResourceGuardError(ValueError):
    pass


# Pure protocol, guard and assembly functions.


def frozen_queries(lines, query_date: str) -> list[dict]:
    queries = [
        row
        for row in (json.loads(line) for line in lines if line.strip())
        if row["occurred_at"].startswith(query_date)
    ]
    if not queries:
        raise ValueError("no frozen queries for the declared date")
    ids = [q["transaction_id"] for q in queries]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate frozen query")
    return queries


def parse_stores(declared: list[str], measured: dict) -> dict:
    """Declared sizes are explicit operator inputs; measured sizes come from the volumes."""
    stores = {name: {"bytes": size, "source": "measured"} for name, size in measured.items()}
    for item in declared:
        name, separator, value = item.partition("=")
        if not separator or not name or not value.isdigit():
            raise ValueError("declared store size must be NAME=BYTES")
        if name in stores:
            raise ValueError(f"store {name} declared twice")
        stores[name] = {"bytes": int(value), "source": "declared"}
    return dict(sorted(stores.items()))


def resource_guard(
    *,
    free_bytes: int,
    artifact_bytes: int,
    stores: dict,
    free_floor: int = FREE_FLOOR,
    derived_cap: int = DERIVED_CAP,
) -> dict:
    record = {
        "free_bytes": free_bytes,
        "artifact_bytes": artifact_bytes,
        "stores": stores,
        "derived_bytes": artifact_bytes + sum(s["bytes"] for s in stores.values()),
        "free_floor_bytes": free_floor,
        "derived_cap_bytes": derived_cap,
    }
    if free_bytes < free_floor:
        raise ResourceGuardError(f"free disk below floor: {json.dumps(record)}")
    if record["derived_bytes"] > derived_cap:
        raise ResourceGuardError(f"derived data above cap: {json.dumps(record)}")
    return record


def disk_reconciliation(
    *,
    artifact_bytes: int,
    volume_bytes: dict,
    free_bytes: int,
    containers: list,
    free_floor: int = FREE_FLOOR,
    derived_cap: int = DERIVED_CAP,
) -> dict:
    record = {
        "schema_version": RECONCILIATION_SCHEMA,
        "artifact_bytes": artifact_bytes,
        "volume_bytes": dict(sorted(volume_bytes.items())),
        "total_derived_bytes": artifact_bytes + sum(volume_bytes.values()),
        "free_bytes": free_bytes,
        "free_floor_bytes": free_floor,
        "derived_cap_bytes": derived_cap,
        "containers": containers,
    }
    if free_bytes < free_floor or record["total_derived_bytes"] > derived_cap:
        raise ResourceGuardError(f"resource limits exceeded: {json.dumps(record)}")
    return record


def parse_du(rows, volumes: list[str]) -> dict:
    sizes = {}
    for row in rows:
        kibibytes, path = row.split(maxsplit=1)
        sizes[volumes[int(path.rsplit("/", 1)[-1])]] = int(kibibytes) * 1024
    if len(sizes) != len(volumes):
        raise ValueError("volume measurement incomplete")
    return sizes


def protocol_declaration(
    queries: list[dict], candidate_ids: dict, scaler_id: str, resources: dict
) -> dict:
    populations = [
        {
            "query_id": q["transaction_id"],
            "candidate_count": len(candidate_ids[q["transaction_id"]]),
            "candidate_ids": candidate_ids[q["transaction_id"]],
            "candidate_hash": content_id(candidate_ids[q["transaction_id"]]),
        }
        for q in queries
    ]
    union = sorted({i for ids in candidate_ids.values() for i in ids})
    declaration = {
        "schema_version": PROTOCOL_SCHEMA,
        "dataset_simulated": True,
        "queries": queries,
        "candidate_union_count": len(union),
        "candidate_union_hash": content_id(union),
        "populations": populations,
        "scaler_id": scaler_id,
        "query_schedule": "first-after-database-restart then five warm repetitions; "
        "alternating SQL/Cypher order; frozen query order",
        "timed_work": "complete neighbourhood canonical documents plus shared merchant "
        "identity, client materialized; no features, vectors or GDS inside timed region",
        "host_cold": False,
        "resources": resources,
        "provider_calls": 0,
    }
    declaration["protocol_id"] = content_id(declaration)
    return declaration


def check_protocol(protocol: dict) -> dict:
    body = {k: v for k, v in protocol.items() if k != "protocol_id"}
    if protocol.get("schema_version") != PROTOCOL_SCHEMA or protocol.get(
        "protocol_id"
    ) != content_id(body):
        raise ValueError("protocol content does not match protocol_id")
    return protocol


def _check_report_id(document: dict) -> dict:
    body = {k: v for k, v in document.items() if k != "report_id"}
    if "report_id" in document and document["report_id"] != content_id(body):
        raise ValueError("observation report_id does not match its content")
    return body


def vector_observations(queries: list[dict], store, repetitions: int = 6) -> list[dict]:
    results = []
    for case in queries:
        times, result = [], None
        for repetition in range(repetitions):
            start = perf_counter()
            found = store.top_five(case)
            times.append((perf_counter() - start) * 1000)
            if repetition and found != result:
                raise ValueError("vector result changed during benchmark")
            result = found
        results.append(
            {
                "query_id": case["transaction_id"],
                "top_five": result,
                "latency_ms": times,
                "candidate_coverage_complete": result is not None,
            }
        )
    return results


def measure_observation(protocol, sql, graph, *, exclusions, resources, first_pass_state):
    """Runtime-role measurement; exclusions/resources are read after all timed work."""
    check_protocol(protocol)
    queries = protocol["queries"]
    report = benchmark_queries(queries, sql, graph)
    vector = vector_observations(queries, sql)
    report.update(
        dataset_simulated=True,
        measurement_mode="measured-local-retrieval",
        protocol_id=protocol["protocol_id"],
        provider_calls=0,
        query_ids=[q["transaction_id"] for q in queries],
        vector_results=vector,
        unsupported_exclusions=[
            {"query_id": q["transaction_id"], "count": exclusions(q)} for q in queries
        ],
        scaler_id=protocol["scaler_id"],
        resources=resources(),
    )
    report["cache_protocol"]["first_pass"] = first_pass_state
    report["vector_latency_ms"] = {
        "first_pass": distribution([r["latency_ms"][0] for r in vector]),
        "warm": distribution([v for r in vector for v in r["latency_ms"][1:]]),
    }
    # Complete memberships are preserved separately; the report keeps counts and hashes.
    memberships = deepcopy(report["queries"])
    for row in report["queries"]:
        for key in ("sql_members", "cypher_members", "sql_candidates", "cypher_candidates"):
            value = row.pop(key)
            row[key + "_count"] = len(value) if value is not None else None
            row[key + "_hash"] = content_id(value)
    return report, memberships


def replacement_receipt(protocol, observation, sql, graph, *, reason):
    check_protocol(protocol)
    _check_report_id(observation)
    if observation.get("protocol_id") != protocol["protocol_id"]:
        raise ValueError("observation belongs to a different protocol")
    rows = []
    for case, original in zip(protocol["queries"], observation["queries"], strict=True):
        if case["transaction_id"] != original["transaction_id"]:
            raise ValueError("replacement query order differs from observation")
        row = {"transaction_id": case["transaction_id"]}
        for name, store in (("sql", sql), ("cypher", graph)):
            start = perf_counter()
            members = store.neighbourhood(case)
            row[name + "_ms"] = (perf_counter() - start) * 1000
            row[name + "_members_hash"] = content_id(members)
            if row[name + "_members_hash"] != original[name + "_members_hash"]:
                raise ValueError("replacement membership differs from measured observation")
        rows.append(row)
    receipt = {
        "schema_version": REPLACEMENT_SCHEMA,
        "protocol_id": protocol["protocol_id"],
        "queries": rows,
        "host_cold": False,
        "reason": reason,
        "warm_repeated": False,
    }
    receipt["receipt_id"] = content_id(receipt)
    return receipt


def _check_annotations(annotations: dict) -> dict:
    if annotations.get("schema_version") != ANNOTATION_SCHEMA:
        raise ValueError("annotations require schema_version " + ANNOTATION_SCHEMA)
    if set(annotations) - ANNOTATION_KEYS:
        unknown = sorted(set(annotations) - ANNOTATION_KEYS)
        raise ValueError("unknown annotation fields: " + ", ".join(unknown))
    limitations = annotations.get("limitations")
    if not limitations or not all(isinstance(x, str) and x for x in limitations):
        raise ValueError("annotations require nonempty limitations")
    if not isinstance(annotations.get("relevance_interpretation"), str):
        raise ValueError("annotations require relevance_interpretation")
    return annotations


def _annotation(annotations, key):
    if not isinstance(annotations.get(key), str) or not annotations[key]:
        raise ValueError(f"annotations require {key}")
    return annotations[key]


def _agreement(result, label):
    return [(c["verdict"] == "decline") == (label == "fraud") for c in result["top_five"]]


def join_labels(vector_results: list[dict], labels: dict) -> list[dict]:
    """Evaluator join after retrieval; labels never enter a runtime query."""
    joined = []
    for result in vector_results:
        label = labels.get(result["query_id"])
        if label is None:
            raise ValueError("missing evaluator label for a measured query")
        if result.get("query_label", label) != label:
            raise ValueError("observation label disagrees with evaluator labels")
        matches = _agreement(result, label) if result["top_five"] else None
        joined.append(
            {
                **result,
                "query_label": label,
                "label_agreement": sum(matches) / len(matches) if matches else None,
            }
        )
    return joined


def relevance_proxy(vector_results: list[dict], interpretation: str) -> dict:
    returned = [r for r in vector_results if r["top_five"]]

    def counts(rows):
        pairs = sum(len(r["top_five"]) for r in rows)
        return pairs, sum(sum(_agreement(r, r["query_label"])) for r in rows)

    pairs, matches = counts(returned)
    fraud = counts([r for r in returned if r["query_label"] == "fraud"])
    legitimate = counts([r for r in returned if r["query_label"] == "legitimate"])
    return {
        "empty_queries": sum(r["top_five"] == [] for r in vector_results),
        "unavailable_queries": sum(r["top_five"] is None for r in vector_results),
        "returned_pairs": pairs,
        "matching_pairs": matches,
        "fraction": matches / pairs if pairs else None,
        "fraud_query_returned": fraud[0],
        "fraud_query_matches": fraud[1],
        "legitimate_query_returned": legitimate[0],
        "legitimate_query_matches": legitimate[1],
        "interpretation": interpretation,
    }


def apply_replacement(body: dict, receipt: dict, first_pass_protocol: str) -> None:
    if receipt.get("schema_version") != REPLACEMENT_SCHEMA or receipt.get(
        "receipt_id"
    ) != content_id({k: v for k, v in receipt.items() if k != "receipt_id"}):
        raise ValueError("replacement receipt content does not match receipt_id")
    if receipt["protocol_id"] != body["protocol_id"]:
        raise ValueError("replacement belongs to a different protocol")
    rows = receipt["queries"]
    if [r["transaction_id"] for r in rows] != [q["transaction_id"] for q in body["queries"]]:
        raise ValueError("replacement query order differs from observation")
    for row, query in zip(rows, body["queries"], strict=True):
        for name in ("sql", "cypher"):
            if row[name + "_members_hash"] != query[name + "_members_hash"]:
                raise ValueError("replacement membership differs from measured observation")
    body["replacement_receipt_id"] = receipt["receipt_id"]
    body["cache_protocol"]["original_first_pass_valid"] = False
    body["cache_protocol"]["first_pass"] = first_pass_protocol
    body["invalid_original_first_pass_ms"] = {
        name: body["latency_ms"][name]["first_pass"] for name in ("sql", "cypher")
    }
    for name in ("sql", "cypher"):
        body["latency_ms"][name]["first_pass"] = distribution([r[name + "_ms"] for r in rows])
        for row, query in zip(rows, body["queries"], strict=True):
            query["timings"][name + "_ms"][0] = row[name + "_ms"]


def memory_bytes(usage: str) -> int:
    """Parse the used side of docker stats MemUsage, e.g. ``2.056GiB / 4GiB``."""
    used = usage.split("/", 1)[0].strip()
    for unit, factor in sorted(UNITS.items(), key=lambda item: -len(item[0])):
        number = used.removesuffix(unit)
        if number != used:
            try:
                return int(float(number) * factor)
            except ValueError:
                break
    raise ValueError(f"unrecognised memory usage {usage!r}")


def sample_maxima(rows: list[dict]) -> dict:
    maxima = {}
    for row in rows:
        maxima[row["Name"]] = max(maxima.get(row["Name"], 0), memory_bytes(row["MemUsage"]))
    return dict(sorted(maxima.items()))


def artifact_record(path: Path) -> dict:
    digest, size, newlines, last = sha256(), 0, 0, b""
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
            newlines += chunk.count(b"\n")
            last = chunk[-1:]
    record = {"sha256": digest.hexdigest(), "bytes": size}
    if Path(path).suffix == ".jsonl":
        record["rows"] = newlines + (1 if size and last != b"\n" else 0)
    return record


def assemble_report(
    observation,
    protocol,
    *,
    labels,
    annotations,
    artifacts,
    replacement=None,
    resources=None,
    cgroup=None,
    samples=None,
):
    """Evaluator assembly of stored observations; annotations are copied verbatim."""
    check_protocol(protocol)
    _check_annotations(annotations)
    body = deepcopy(_check_report_id(observation))
    if (
        body.get("schema_version") != RETRIEVAL_SCHEMA
        or body.get("measurement_mode") not in MEASUREMENT_MODES
    ):
        raise ValueError("observation needs a labelled retrieval benchmark measurement_mode")
    if body.get("protocol_id") != protocol["protocol_id"] or body.get("query_ids") != [
        q["transaction_id"] for q in protocol["queries"]
    ]:
        raise ValueError("observation does not belong to the frozen protocol")
    if "report_id" in observation:
        body["source_report_id"] = observation["report_id"]
    body["vector_results"] = join_labels(body.get("vector_results", []), labels)
    body["vector_relevance_proxy"] = relevance_proxy(
        body["vector_results"], annotations["relevance_interpretation"]
    )
    body["limitations"] = list(annotations["limitations"])
    if "plan_observation" in annotations:
        body["plan_observation"] = _annotation(annotations, "plan_observation")
    if replacement is not None:
        apply_replacement(body, replacement, _annotation(annotations, "first_pass_protocol"))
    if resources is not None:
        body["resources"] = {
            **resources,
            "accounting_stage": _annotation(annotations, "resource_accounting_stage"),
        }
    if cgroup is not None:
        body["cgroup_current_lifetime"] = cgroup
    if samples:
        body["service_sample_maximum_bytes"] = {
            name: sample_maxima(rows) for name, rows in samples.items()
        }
    body["artifacts"] = dict(sorted(artifacts.items()))
    return body


# Store-touching orchestration. Kept thin; covered by integration-marked tests.


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


def _tree_bytes(root: Path) -> int:
    return sum(p.stat().st_size for p in Path(root).rglob("*") if p.is_file())


def volume_bytes(volumes: dict, image: str) -> dict:
    """Measure named volumes read-only, so stopped stores are measured, never assumed."""
    names = sorted(volumes)
    command = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--memory=128m",
        "--entrypoint",
        "du",
    ]
    for index, name in enumerate(names):
        command += ["-v", f"{volumes[name]}:/audit/{index}:ro"]
    command += [image, "-sk", *[f"/audit/{index}" for index in range(len(names))]]
    rows = subprocess.check_output(command, text=True).splitlines()
    return parse_du(rows, names)


class _Guard:
    def __init__(self, args):
        if args.store_volume and not args.du_image:
            raise ValueError("--store-volume requires --du-image")
        self.args = args
        self.volumes = dict(item.split("=", 1) for item in args.store_volume)

    def __call__(self, connection=None) -> dict:
        measured = volume_bytes(self.volumes, self.args.du_image) if self.volumes else {}
        record = resource_guard(
            free_bytes=shutil.disk_usage(self.args.output_dir).free,
            artifact_bytes=_tree_bytes(self.args.artifact_root),
            stores=parse_stores(self.args.store_bytes, measured),
            free_floor=self.args.free_floor_bytes,
            derived_cap=self.args.derived_cap_bytes,
        )
        if connection is not None:
            record["database_bytes"] = connection.execute(
                "SELECT pg_database_size(current_database())"
            ).fetchone()[0]
        return record


class _Sampler:
    """Coarse docker stats observations; they are samples, not exact peaks."""

    def __init__(self, path: Path, containers: list[str], interval: float):
        self.path, self.containers, self.interval = path, containers, interval
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run)
        self.started = time.perf_counter()

    def _run(self):
        with self.path.open("x") as handle:
            while not self.stop_event.is_set():
                raw = subprocess.check_output(
                    ["docker", "stats", "--no-stream", "--format", "{{json .}}", *self.containers],
                    text=True,
                )
                elapsed = time.perf_counter() - self.started
                for line in raw.splitlines():
                    handle.write(json.dumps({"elapsed_seconds": elapsed, **json.loads(line)}))
                    handle.write("\n")
                handle.flush()
                self.stop_event.wait(self.interval)

    def __enter__(self):
        if self.containers:
            self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop_event.set()
        if self.containers:
            self.thread.join()


def _scaler(args, protocol=None) -> dict:
    scaler = _json(args.scaler)
    if protocol is not None and scaler["scaler_id"] != protocol["scaler_id"]:
        raise ValueError("scaler differs from the frozen protocol")
    return scaler


def _credentials(args):
    return _env(args.pg_dsn_env), (_env(args.neo4j_user_env), _env(args.neo4j_password_env))


def _stores(args, scaler, credentials):
    from neo4j import GraphDatabase

    from reckoner.v1.benchmark.queries import CypherQueries, SQLQueries

    dsn, auth = credentials
    driver = GraphDatabase.driver(args.neo4j_uri, auth=auth, warn_notification_severity="OFF")
    driver.verify_connectivity()
    return driver, SQLQueries(dsn, scaler), CypherQueries(driver)


def _inventory(args):
    import psycopg

    dsn = _env(args.pg_dsn_env)
    output = Path(args.output_dir)
    queries = frozen_queries(
        (Path(args.bundle) / "runtime_validation.jsonl").read_text().splitlines(), args.query_date
    )
    scaler, guard = _scaler(args), _Guard(args)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("SET default_transaction_read_only=on")
        guard(connection)  # Refuse before writing large outputs.
        members, candidate_ids = {}, {}
        for q in queries:
            at = q["occurred_at"]
            rows = connection.execute(INVENTORY_SQL, (q["tenant_id"], at, at, at)).fetchall()
            candidate_ids[q["transaction_id"]] = [r[0]["transaction_id"] for r in rows]
            members.update((r[0]["transaction_id"], r[0]) for r in rows)
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
            queries, candidate_ids, scaler["scaler_id"], guard(connection)
        )
        _write_new(output / "protocol.json", protocol)
        plan = connection.execute(
            "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT * FROM reckoner.v1_neighbours(%s,%s)",
            (queries[0]["tenant_id"], queries[0]["transaction_id"]),
        ).fetchone()[0]
        _write_new(output / "sql-function-plan.json", plan)
    return {
        k: protocol[k] for k in ("candidate_union_count", "candidate_union_hash", "protocol_id")
    }


def _prepare(args):
    import psycopg

    from reckoner.v1.benchmark.queries import candidate_vectors
    from reckoner.v1.data.history import SourceHistory

    dsn = _env(args.pg_dsn_env)
    output = Path(args.output_dir)
    protocol = check_protocol(_json(output / "protocol.json"))
    scaler, guard = _scaler(args, protocol), _Guard(args)
    candidates = [
        json.loads(line) for line in (output / "candidates.jsonl").read_text().splitlines()
    ]
    ids = sorted(tx["transaction_id"] for tx in candidates)
    if (
        len(ids) != protocol["candidate_union_count"]
        or content_id(ids) != protocol["candidate_union_hash"]
    ):
        raise ValueError("candidate file differs from the frozen protocol union")
    started, count, samples, batch = perf_counter(), 0, [], []

    def flush(connection):
        with connection.cursor().copy("COPY reckoner.v1_vectors FROM STDIN") as copy:
            for row in batch:
                copy.write_row(row)
        connection.commit()
        batch.clear()
        samples.append(guard(connection))

    with (
        SourceHistory(args.bundle, args.source_dir) as history,
        psycopg.connect(dsn) as connection,
        (output / "vectors.jsonl").open("x") as handle,
    ):
        samples.append(guard(connection))
        for tx, vector in candidate_vectors(candidates, history, scaler):
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
            batch.append(
                (
                    tx["tenant_id"],
                    tx["transaction_id"],
                    scaler["scaler_id"],
                    "features-v1",
                    str(vector),
                )
            )
            count += 1
            if len(batch) >= args.batch_size:
                flush(connection)
                handle.flush()
        if batch:
            flush(connection)
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
    protocol = check_protocol(_json(output / "protocol.json"))
    scaler, guard = _scaler(args, protocol), _Guard(args)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("SET default_transaction_read_only=on")
        stored = [
            r[0]
            for r in connection.execute(
                "SELECT transaction_id FROM reckoner.v1_vectors WHERE scaler_id=%s "
                "ORDER BY transaction_id",
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
    if (
        len(stored) != protocol["candidate_union_count"]
        or content_id(stored) != protocol["candidate_union_hash"]
    ):
        raise ValueError("stored vectors differ from the frozen protocol union")
    if not all(r["complete"] for r in coverage):
        raise ValueError("strict vector coverage incomplete")
    result = {
        "stored_count": len(stored),
        "stored_hash": content_id(stored),
        "coverage": coverage,
        "resources": resources,
    }
    _write_new(output / "stored-verification.json", result)
    return {k: result[k] for k in ("stored_count", "stored_hash")}


def _measure(args):
    credentials = _credentials(args)
    output = Path(args.output_dir)
    protocol = check_protocol(_json(output / "protocol.json"))
    scaler, guard = _scaler(args, protocol), _Guard(args)
    driver, sql, graph = _stores(args, scaler, credentials)
    try:
        with _Sampler(output / "service-samples.jsonl", args.sample_container, args.interval):
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
            )
    finally:
        sql.close()
        graph.close()
        driver.close()
    with (output / "exact-memberships.json").open("x") as handle:
        json.dump(memberships, handle)
    report = write_report(observation, output / "retrieval-report")
    return {"report_id": report["report_id"], "latency_ms": report["latency_ms"]}


def _replacement(args):
    credentials = _credentials(args)
    output = Path(args.output_dir)
    protocol = check_protocol(_json(output / "protocol.json"))
    observation = _json(output / "retrieval-report.json")
    scaler = _scaler(args, protocol)
    driver, sql, graph = _stores(args, scaler, credentials)
    try:
        with _Sampler(
            output / "replacement-service-samples.jsonl", args.sample_container, args.interval
        ):
            receipt = replacement_receipt(protocol, observation, sql, graph, reason=args.reason)
    finally:
        sql.close()
        graph.close()
        driver.close()
    _write_new(output / "replacement-first-pass.json", receipt)
    return {"receipt_id": receipt["receipt_id"]}


def _docker(*command) -> str:
    return subprocess.check_output(["docker", *command], text=True)


def _audit(args):
    output = Path(args.output_dir)
    names = _docker(
        "ps", "-a", "--filter", "name=" + args.container_prefix, "--format", "{{.Names}}"
    ).splitlines()
    inspected = json.loads(_docker("inspect", *names)) if names else []
    volumes = sorted({m["Name"] for c in inspected for m in c["Mounts"] if m["Type"] == "volume"})
    result = disk_reconciliation(
        artifact_bytes=_tree_bytes(args.artifact_root),
        volume_bytes=volume_bytes({v: v for v in volumes}, args.du_image) if volumes else {},
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
    guarded(steps.add_parser("verify"))
    measure = steps.add_parser("measure")
    guarded(measure)
    graph(measure)
    measure.add_argument("--first-pass-state", required=True)
    replacement = steps.add_parser("replacement")
    replacement.add_argument("--output-dir", type=Path, required=True)
    replacement.add_argument("--scaler", type=Path, required=True)
    replacement.add_argument("--pg-dsn-env", required=True, metavar="ENV_VAR_NAME")
    replacement.add_argument("--reason", required=True)
    graph(replacement)
    audit = steps.add_parser("audit")
    audit.add_argument("--output-dir", type=Path, required=True)
    audit.add_argument("--artifact-root", type=Path, required=True)
    audit.add_argument("--container-prefix", required=True)
    audit.add_argument("--du-image", required=True)
    audit.add_argument("--label", required=True)
    audit.add_argument("--cgroup-container", action="append", default=[], metavar="NAME=CONTAINER")
    limits(audit)
    report = steps.add_parser("report")
    for name in ("--protocol", "--observation", "--oracle-labels", "--annotations", "--output"):
        report.add_argument(name, type=Path, required=True)
    for name in ("--replacement", "--resources", "--cgroup"):
        report.add_argument(name, type=Path)
    report.add_argument("--samples", type=Path, action="append", default=[])
    report.add_argument("--attach", type=Path, action="append", default=[])

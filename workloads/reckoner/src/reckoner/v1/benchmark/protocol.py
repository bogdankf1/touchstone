"""Frozen retrieval protocol and the runtime observations it schedules.

`INVENTORY_SQL` is the only privileged oracle-resolution read in the benchmark; it is
used by the evaluator-side `inventory` step. Measurement functions here never see labels.
"""

import json
from copy import deepcopy
from time import perf_counter

from reckoner.contracts import content_id
from reckoner.v1.benchmark.queries import benchmark_queries, distribution

PROTOCOL_SCHEMA = "retrieval-protocol-v1"
REPLACEMENT_SCHEMA = "retrieval-replacement-first-pass-v1"
# Declared in every new protocol; the frozen Task 11 protocol predates this field.
CANDIDATE_POLICY = {
    "resolution_policy_version": "simulated-seven-days-v1",
    "resolution_window_days": 90,
    "same_tenant": True,
    "occurred_strictly_before_query": True,
    "resolved_strictly_before_query": True,
    "positive_amount_only": True,
}
INVENTORY_SQL = (
    "SELECT h.document FROM oracle.v1_resolutions r JOIN reckoner.v1_history h "
    "USING(tenant_id,transaction_id) WHERE r.tenant_id=%s "
    "AND r.resolution_policy_version=%s "
    "AND r.resolved_at<%s::timestamptz "
    "AND r.resolved_at>=%s::timestamptz-make_interval(days=>%s) "
    "AND h.occurred_at<%s::timestamptz AND (h.document->>'amount_minor')::bigint>0"
)


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


def inventory_parameters(query: dict) -> tuple:
    at = query["occurred_at"]
    return (
        query["tenant_id"],
        CANDIDATE_POLICY["resolution_policy_version"],
        at,
        at,
        CANDIDATE_POLICY["resolution_window_days"],
        at,
    )


def inventory_population(rows_by_query: dict) -> tuple[dict, dict]:
    """Python ordering (not database collation) feeds every hash; members keyed by tenant."""
    candidate_ids, members, tenants = {}, {}, {}
    for query_id, rows in rows_by_query.items():
        candidate_ids[query_id] = sorted(row["transaction_id"] for row in rows)
        for row in rows:
            if tenants.setdefault(row["transaction_id"], row["tenant_id"]) != row["tenant_id"]:
                # Frozen union hashes are over transaction IDs, so they must be unambiguous.
                raise ValueError("transaction_id appears under two tenants")
            members[(row["tenant_id"], row["transaction_id"])] = row
    return candidate_ids, members


def check_stored(stored_ids: list[str], coverage: list[dict], protocol: dict) -> dict:
    stored = sorted(stored_ids)
    if (
        len(stored) != protocol["candidate_union_count"]
        or content_id(stored) != protocol["candidate_union_hash"]
    ):
        raise ValueError("stored vectors differ from the frozen protocol union")
    if not all(row["complete"] for row in coverage):
        raise ValueError("strict vector coverage incomplete")
    return {"stored_count": len(stored), "stored_hash": content_id(stored), "coverage": coverage}


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
        "candidate_policy": dict(CANDIDATE_POLICY),
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


def check_report_id(document: dict) -> dict:
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


def measure_observation(protocol, sql, graph, *, exclusions, resources, first_pass_state, runtime):
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
        runtime_identity=runtime,
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


def replacement_receipt(protocol, observation, sql, graph, *, reason, runtime, resources=None):
    check_protocol(protocol)
    check_report_id(observation)
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
        "runtime_identity": runtime,
    }
    if resources is not None:
        receipt["resources"] = resources()
    receipt["receipt_id"] = content_id(receipt)
    return receipt

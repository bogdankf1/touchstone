"""Common bounded display and schema-valid evidence assembly."""

from neo4j.exceptions import ServiceUnavailable, SessionExpired

from reckoner.contracts import content_id
from reckoner.v1.contracts import validate_v1
from reckoner.v1.data.history import instant


def query_transaction(task):
    tx = task["transaction"]
    if instant(task.get("query_time", tx["occurred_at"])) != instant(tx["occurred_at"]):
        raise ValueError("query time must match canonical transaction")
    return tx


def neighbourhood(rows):
    nodes, edges, merchants = {}, {}, {}
    for item in rows:
        tx = item["transaction"]
        ids = {}
        for kind in ("account", "card", "merchant", "transaction"):
            identity = tx[f"{kind}_id"]
            key = content_id([tx["tenant_id"], kind, identity])
            ids[kind] = key
            nodes[key] = {
                "id": key,
                "tenant_id": tx["tenant_id"],
                "kind": kind,
                "identity": identity,
            }
        for source, target, kind in (
            ("account", "card", "owns"),
            ("card", "transaction", "used"),
            ("transaction", "merchant", "paid"),
        ):
            key = content_id([ids[source], ids[target], kind])
            edges[key] = {"id": key, "source": ids[source], "target": ids[target], "kind": kind}
        shared = item.get("merchant_identity")
        if shared is not None:
            merchants.setdefault(shared, set()).add(ids["merchant"])
    for occurrences in merchants.values():
        for a in sorted(occurrences):
            for b in sorted(occurrences):
                if a < b and nodes[a]["tenant_id"] != nodes[b]["tenant_id"]:
                    key = content_id([a, b, "shared_identity"])
                    edges[key] = {"id": key, "source": a, "target": b, "kind": "shared_identity"}
    display_nodes = [nodes[key] for key in sorted(nodes)[:100]]
    included = {n["id"] for n in display_nodes}
    display_edges = [
        e for _, e in sorted(edges.items()) if e["source"] in included and e["target"] in included
    ][:200]
    return {
        "window_days": 30,
        "total_nodes": len(nodes),
        "total_edges": len(edges),
        "total_transactions": len(rows),
        "truncated": len(display_nodes) < len(nodes) or len(display_edges) < len(edges),
        "cross_tenant": len({r["transaction"]["tenant_id"] for r in rows}) > 1,
        "nodes": display_nodes,
        "edges": display_edges,
        "transaction_refs": sorted(
            content_id(
                [r["transaction"]["tenant_id"], "transaction", r["transaction"]["transaction_id"]]
            )
            for r in rows
        ),
    }


def document(
    tx, snapshot, coverage, features, indicators=None, cases=None, display=None, projection=None
):
    body = {
        "schema_version": "reckoner-evidence-v1",
        "tenant_id": tx["tenant_id"],
        "transaction_id": tx["transaction_id"],
        "query_time": tx["occurred_at"],
        "source_snapshot_ids": {
            "postgres": snapshot,
            "graph": projection["projection_id"] if projection else None,
        },
        "cutoffs": {
            "history_before": tx["occurred_at"],
            "resolved_before": tx["occurred_at"],
            "graph_before": projection["cutoff"] if projection else None,
        },
        "coverage": coverage,
        "features": features,
        "risk_indicators": indicators or [],
        "comparable_cases": cases or [],
    }
    if display is not None:
        body["neighbourhood"] = display
    if projection is not None:
        body["graph_projection"] = projection
    return validate_v1("evidence", {**body, "evidence_id": content_id(body)})


def assemble_evidence(task: dict, config: dict, relational, graph) -> dict:
    tx = query_transaction(task)
    base = relational.for_task(task, config)
    unreachable = False
    try:
        extra = graph.for_task(task, config) if graph is not None else None
    except (ServiceUnavailable, SessionExpired):
        # A configured but stopped graph service degrades to explicit partial evidence;
        # the relational arm continues and a GDS-augmented run cannot present as complete.
        extra, unreachable = None, True
    missing = list(base["coverage"]["missing"])
    if unreachable:
        missing.append("graph unavailable: service unreachable")
    elif extra is None:
        missing.append("graph unavailable")
    else:
        missing.extend(extra["coverage"]["missing"])
    return document(
        tx,
        base["source_snapshot_ids"]["postgres"],
        {
            "status": "unavailable"
            if base["coverage"]["status"] == "unavailable"
            else ("partial" if missing else "available"),
            "missing": sorted(set(missing)),
        },
        {**base["features"], **(extra["features"] if extra else {})},
        base["risk_indicators"],
        base["comparable_cases"],
        base.get("neighbourhood"),
        extra.get("graph_projection") if extra else None,
    )

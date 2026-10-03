"""Assert the fabricated Reckoner v1 smoke from inside the deployment network.

Runs in the Reckoner image (stdlib, psycopg, neo4j). Dataset is fabricated simulated data;
no provider is called. Subcommands write one JSON receipt and exit non-zero on failure:

  fingerprint  Postgres table hashes (owner DSN) and, with --graph, the source graph
  online       API/web readiness, tenant-scoped cases, degraded graph state, proxy rules
  stopped      Postgres stopped: liveness stays up, readiness and data reads fail closed
  refresh      Touchstone published the fabricated run through OTLP and the warehouse
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

RUN_ID = "fabricated-smoke-v1"
TENANTS = ("tenant-a", "tenant-b")
SCHEMAS = ("reckoner", "oracle")
DETERMINISTIC = (
    "cutoff",
    "node_count",
    "edge_count",
    "covered_accounts",
    "covered_cards",
    "covered_merchants",
    "community_count",
    "page_rank_converged",
    "page_rank_iterations",
)


def _get(url, *, timeout=10):
    try:
        with urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode()
    except HTTPError as error:
        return error.code, error.read().decode()


def _json(url):
    status, body = _get(url)
    if status != 200:
        raise AssertionError(f"{url} returned {status}")
    return json.loads(body)


def _secrets():
    """Credential values that must never appear in an HTTP response."""
    from psycopg.conninfo import conninfo_to_dict

    values = []
    for key in ("RECKONER_OWNER_DSN", "RECKONER_RUNNER_DSN", "RECKONER_API_DSN"):
        if os.environ.get(key):
            password = conninfo_to_dict(os.environ[key]).get("password")
            if password:
                values.append(password)
    if os.environ.get("RECKONER_NEO4J_PASSWORD"):
        values.append(os.environ["RECKONER_NEO4J_PASSWORD"])
    return values


def postgres_fingerprint(dsn):
    import psycopg
    from psycopg import sql

    tables = {}
    with psycopg.connect(dsn) as connection:
        names = connection.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_type='BASE TABLE' AND (table_schema = ANY(%s) OR (table_schema='public' "
            "AND table_name='reckoner_schema_migrations')) ORDER BY 1, 2",
            (list(SCHEMAS),),
        ).fetchall()
        for schema, table in names:
            rows = sorted(
                row[0]
                for row in connection.execute(
                    sql.SQL("SELECT t::text FROM {}.{} t").format(
                        sql.Identifier(schema), sql.Identifier(table)
                    )
                )
            )
            digest = hashlib.sha256("\n".join(rows).encode()).hexdigest()
            tables[f"{schema}.{table}"] = {"rows": len(rows), "sha256": digest}
        roles = sorted(
            r[0]
            for r in connection.execute(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE 'reckoner%' ORDER BY 1"
            )
        )
        extension = connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname='vector'"
        ).fetchone()
    return {"tables": tables, "roles": roles, "vector_extension": extension[0]}


def graph_fingerprint():
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(
        os.environ["RECKONER_NEO4J_URI"],
        auth=(os.environ["RECKONER_NEO4J_USER"], os.environ["RECKONER_NEO4J_PASSWORD"]),
        warn_notification_severity="OFF",
    )
    with driver, driver.session() as session:
        source = sorted(
            json.dumps([r["labels"], r["properties"]], sort_keys=True, default=str)
            for r in session.run(
                "MATCH (n) WHERE n.tenant_id IN $tenants AND NOT n:ProjectionReceipt "
                "AND NOT n:GDSMetric RETURN labels(n) AS labels, properties(n) AS properties",
                tenants=list(TENANTS),
            )
        )
        edges = sorted(
            json.dumps([r["type"], r["start"], r["end"]], sort_keys=True, default=str)
            for r in session.run(
                "MATCH (a)-[r]->(b) WHERE a.tenant_id IN $tenants RETURN type(r) AS type, "
                "coalesce(a.key, a.transaction_id, a.merchant_id) AS start, "
                "coalesce(b.key, b.transaction_id, b.merchant_id) AS end",
                tenants=list(TENANTS),
            )
        )
        receipts = [
            json.loads(r["document"])
            for r in session.run(
                "MATCH (p:ProjectionReceipt) RETURN DISTINCT p.document AS document"
            )
        ]
    return {
        "source_nodes": len(source),
        "source_sha256": hashlib.sha256("\n".join(source).encode()).hexdigest(),
        "relationships": len(edges),
        "relationships_sha256": hashlib.sha256("\n".join(edges).encode()).hexdigest(),
        "projections": sorted(
            ({k: receipt[k] for k in DETERMINISTIC} for receipt in receipts),
            key=lambda item: json.dumps(item, sort_keys=True),
        ),
    }


def fingerprint(args):
    result = {"postgres": postgres_fingerprint(os.environ["RECKONER_OWNER_DSN"])}
    if args.graph:
        result["graph"] = graph_fingerprint()
    return result


def online(args):
    checks, secrets = {}, _secrets()
    status, body = _get(args.api + "/health/live")
    checks["api_live"] = status == 200 and json.loads(body) == {"status": "ok"}
    ready = _json(args.api + "/health/ready")
    checks["api_ready_latest_migration"] = ready["status"] == "ready"
    cases = {}
    for tenant in TENANTS:
        query = urlencode({"tenant_id": tenant, "status": "all", "run_id": RUN_ID})
        page = _json(f"{args.api}/v1/cases?{query}")
        proxied = _json(f"{args.web}/api/reckoner/cases?{query}")
        checks[f"{tenant}_web_proxy_matches_api"] = proxied == page
        cases[tenant] = page["items"]
        for item in page["items"]:
            detail = _json(
                f"{args.api}/v1/case?"
                + urlencode({"tenant_id": tenant, "case_id": item["case_id"]})
            )
            graph_stopped = (
                "graph unavailable" in detail["coverage"]["missing"]
                and detail["source_snapshot_ids"]["graph"] is None
            )
            checks[f"{tenant}_{item['case_id'][:12]}_graph_unavailable_disclosed"] = graph_stopped
            checks[f"{tenant}_{item['case_id'][:12]}_degraded"] = (
                detail["degraded_reason"] == "evidence_unavailable"
                and detail["effective_probability"] is None
            )
            other = "tenant-b" if tenant == "tenant-a" else "tenant-a"
            status, _ = _get(
                f"{args.api}/v1/case?" + urlencode({"tenant_id": other, "case_id": item["case_id"]})
            )
            checks[f"{tenant}_{item['case_id'][:12]}_foreign_tenant_404"] = status == 404
    checks["two_cases_per_tenant"] = all(len(items) == 2 for items in cases.values())
    status, page = _get(args.web + "/review")
    checks["web_review_page"] = status == 200
    request = Request(
        args.web + "/api/reckoner/reviews",
        data=b"{}",
        method="POST",
        headers={"Origin": "http://evil.invalid", "Content-Type": "application/json"},
    )
    try:
        urlopen(request, timeout=10)
        checks["web_rejects_foreign_origin_post"] = False
    except HTTPError as error:
        checks["web_rejects_foreign_origin_post"] = error.code == 403
    responses = json.dumps(cases) + page
    checks["no_credentials_in_responses"] = not any(s in responses for s in secrets)
    return {"checks": checks, "case_counts": {t: len(c) for t, c in cases.items()}}


def stopped(args):
    checks = {}
    status, _ = _get(args.api + "/health/live")
    checks["api_live_while_postgres_stopped"] = status == 200
    status, body = _get(args.api + "/health/ready")
    checks["api_not_ready_while_postgres_stopped"] = status == 503
    query = urlencode({"tenant_id": "tenant-a", "status": "all"})
    status, _ = _get(f"{args.api}/v1/cases?{query}")
    checks["api_cases_503"] = status == 503
    status, _ = _get(f"{args.web}/api/reckoner/cases?{query}")
    checks["web_proxy_503"] = status == 503
    checks["no_credentials_in_error"] = not any(s in body for s in _secrets())
    return {"checks": checks}


def refresh(args):
    checks = {}
    ready_status, _ = _get(args.platform_api + "/readyz")
    checks["platform_ready"] = ready_status == 200
    workflows = _json(args.platform_api + "/v1/workflows")["data"]
    checks["reckoner_workflow_published"] = any(
        row["workflow_id"] == "reckoner" for row in workflows
    )
    summaries = {}
    for tenant in TENANTS:
        query = urlencode({"workflow_id": "reckoner", "tenant_id": tenant})
        summaries[tenant] = _json(f"{args.platform_api}/v1/runs/{RUN_ID}/summary?{query}")["data"]
    checks["both_tenants_published"] = all(summaries.values())
    return {"checks": checks, "summaries": summaries}


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    printer = commands.add_parser("fingerprint")
    printer.add_argument("--graph", action="store_true")
    for name in ("online", "stopped"):
        command = commands.add_parser(name)
        command.add_argument("--api", required=True)
        command.add_argument("--web", required=True)
    command = commands.add_parser("refresh")
    command.add_argument("--platform-api", required=True)
    for command in commands.choices.values():
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = {
            "fingerprint": fingerprint,
            "online": online,
            "stopped": stopped,
            "refresh": refresh,
        }[args.command](args)
    except (AssertionError, URLError, OSError, KeyError, ValueError) as error:
        print(
            f"{args.command} verification failed: {type(error).__name__}: {error}", file=sys.stderr
        )
        return 1
    failed = [name for name, passed in result.get("checks", {}).items() if not passed]
    result = {
        "schema_version": "reckoner-v1-smoke-receipt-v1",
        "check": args.command,
        "dataset_simulated": True,
        "provider_calls": 0,
        "failed": failed,
        **result,
    }
    text = json.dumps(result, indent=2, sort_keys=True, default=str) + "\n"
    if str(args.output) == "-":
        sys.stdout.write(text)  # Captured by the orchestrator (kind logs or compose run).
    else:
        with args.output.open("x") as handle:
            handle.write(text)
    print(json.dumps({"check": args.command, "failed": failed}, sort_keys=True), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

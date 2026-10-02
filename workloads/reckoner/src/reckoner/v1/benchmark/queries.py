"""Equal-work neighbourhood timings; eligible sets are a separate correctness check."""

from collections import defaultdict
from datetime import timedelta
from math import ceil
from time import perf_counter

from reckoner.v1.data.history import instant
from reckoner.v1.evidence.features import feature_vector


def candidate_vectors(candidates, history, scaler):
    """Read each complete card window once; every vector uses its own strict cutoff."""
    cards = defaultdict(list)
    for tx in candidates:
        cards[(tx["tenant_id"], tx["card_id"])].append(tx)
    for (tenant, card), members in sorted(cards.items()):
        members.sort(key=lambda tx: (tx["occurred_at"], tx["transaction_id"]))
        start = instant(members[0]["occurred_at"]) - timedelta(days=30)
        end = members[-1]["occurred_at"]
        rows = list(history.card_before(tenant, card, end, since=start.isoformat()))
        previous = history.previous_card(tenant, card, start.isoformat())
        rows.sort(key=lambda tx: (tx["occurred_at"], tx["transaction_id"]))
        offset = 0
        for tx in members:
            cutoff = instant(tx["occurred_at"])
            while offset < len(rows) and instant(rows[offset]["occurred_at"]) < cutoff:
                previous = rows[offset]
                offset += 1
            lower = cutoff - timedelta(days=30)
            window = [r for r in rows[:offset] if instant(r["occurred_at"]) >= lower]
            yield (
                tx,
                feature_vector(
                    tx,
                    {"status": "available", "transactions": window, "previous": previous},
                    scaler,
                ),
            )


def distribution(values):
    ordered = sorted(values)
    return {
        "population": len(values),
        **{
            f"p{p}": ordered[ceil(p / 100 * len(ordered)) - 1] if ordered else None
            for p in (50, 95, 99)
        },
    }


def benchmark_queries(cases: list[dict], relational, graph) -> dict:
    ids = [c["transaction_id"] for c in cases]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("empty or duplicate query membership")
    reports = [
        {"transaction_id": c["transaction_id"], "timings": {"sql_ms": [], "cypher_ms": []}}
        for c in cases
    ]
    for repetition in range(6):
        for case, report in zip(cases, reports, strict=True):
            stores = [("sql", relational), ("cypher", graph)]
            if repetition % 2:
                stores.reverse()
            for name, store in stores:
                start = perf_counter()
                members = store.neighbourhood(case)
                report["timings"][name + "_ms"].append((perf_counter() - start) * 1000)
                if repetition == 0:
                    report[name + "_members"] = members
                elif members != report[name + "_members"]:
                    raise ValueError("read-only result changed during benchmark")
    for case, report in zip(cases, reports, strict=True):
        sql, cypher = relational.candidates(case), graph.candidates(case)
        report["sql_candidates"], report["cypher_candidates"] = sql, cypher
        report["candidate_difference"] = {
            "sql_only": sorted(set(sql or []) - set(cypher or [])),
            "cypher_only": sorted(set(cypher or []) - set(sql or [])),
        }
        report["exact_candidates"] = sql is not None and cypher is not None and sql == cypher
        report["exact_neighbourhood"] = (
            report["sql_members"] is not None
            and report["cypher_members"] is not None
            and report["sql_members"] == report["cypher_members"]
        )
    return {
        "schema_version": "reckoner-retrieval-benchmark-v1",
        "query_count": len(cases),
        "queries": reports,
        "exact_neighbourhood_matches": sum(r["exact_neighbourhood"] for r in reports),
        "exact_candidate_matches": sum(r["exact_candidates"] for r in reports),
        "uncovered_queries": [
            r["transaction_id"]
            for r in reports
            if r["sql_members"] is None or r["cypher_members"] is None
        ],
        "latency_ms": {
            name: {
                "first_pass": distribution([r["timings"][name + "_ms"][0] for r in reports]),
                "warm": distribution([v for r in reports for v in r["timings"][name + "_ms"][1:]]),
            }
            for name in ("sql", "cypher")
        },
        "cache_protocol": {
            "first_pass": "caller must record restart state",
            "warm_repetitions": 5,
            "alternating_store_order": True,
            "host_cold": False,
        },
        "decision_impact": {
            "status": "unavailable",
            "reason": "identical persisted scoring not supplied; pending approved Task13 run",
        },
    }


class SQLQueries:
    """Read-only benchmark session; complete canonical rows match the Cypher payload."""

    def __init__(self, dsn, scaler):
        import psycopg

        self.connection = psycopg.connect(dsn, autocommit=True)
        self.connection.execute("SET default_transaction_read_only=on")
        self.scaler = scaler

    def close(self):
        self.connection.close()

    def neighbourhood(self, tx):
        key = (tx["tenant_id"], tx["transaction_id"])
        scope = self.connection.execute(
            "SELECT tenant_id FROM reckoner.v1_shared_merchant_scope(%s,%s)", key
        ).fetchall()
        if not scope:
            return None
        cutoff = instant(tx["occurred_at"])
        for tenant in {tx["tenant_id"], *(r[0] for r in scope)}:
            coverage = self.connection.execute(
                "SELECT history_from,history_until FROM reckoner.v1_runtime_coverage WHERE "
                "tenant_id=%s",
                (tenant,),
            ).fetchone()
            if (
                coverage is None
                or coverage[0] > cutoff - timedelta(days=97)
                or coverage[1] < cutoff
            ):
                return None
        return [
            {"transaction": r[0], "merchant_identity": r[1]}
            for r in self.connection.execute("SELECT * FROM reckoner.v1_neighbours(%s,%s)", key)
        ]

    def candidates(self, tx):
        return [
            r[0]["transaction_id"]
            for r in self.connection.execute(
                "SELECT document FROM reckoner.v1_comparable_candidates(%s,%s)",
                (tx["tenant_id"], tx["transaction_id"]),
            )
        ]

    def top_five(self, tx):
        key = (tx["tenant_id"], tx["transaction_id"])
        if not self.connection.execute(
            "SELECT reckoner.v1_vector_coverage(%s,%s,%s)", (*key, self.scaler["scaler_id"])
        ).fetchone()[0]:
            return None
        rows = self.connection.execute(
            "SELECT document FROM reckoner.v1_history_before(%s,%s)", key
        ).fetchall()
        previous = self.connection.execute(
            "SELECT document FROM reckoner.v1_previous_card(%s,%s)", key
        ).fetchone()
        vec = feature_vector(
            tx,
            {
                "status": "available",
                "transactions": [r[0] for r in rows],
                "previous": previous[0] if previous else None,
            },
            self.scaler,
        )
        return [
            {**r[0], "distance": r[1]}
            for r in self.connection.execute(
                "SELECT * FROM reckoner.v1_vector_search(%s,%s,%s::vector,%s)",
                (*key, str(vec), self.scaler["scaler_id"]),
            )
        ]


class CypherQueries:
    def __init__(self, driver):
        self.driver = driver
        self.session = driver.session(default_access_mode="READ")

    def close(self):
        self.session.close()

    def neighbourhood(self, tx):
        import json

        from reckoner.v1.evidence.neo4j import CYPHER

        merchant = self.session.run(
            "MATCH(m:MerchantIdentity {tenant_id:$tenant,merchant_id:$merchant}) RETURN "
            "m.shared_identity AS identity",
            tenant=tx["tenant_id"],
            merchant=tx["merchant_id"],
        ).single()
        if merchant is None:
            return None
        scope = list(
            self.session.run(
                "MATCH(m:MerchantIdentity {shared_identity:$identity}) WITH DISTINCT "
                "m.tenant_id AS tenant OPTIONAL MATCH(c:EvidenceCoverage {tenant_id:tenant}) "
                "RETURN tenant,c.history_from AS start,c.history_until AS end",
                identity=merchant["identity"],
            )
        )
        cutoff = instant(tx["occurred_at"])
        if not scope or any(
            r["start"] is None
            or instant(str(r["start"])) > cutoff - timedelta(days=97)
            or instant(str(r["end"])) < cutoff
            for r in scope
        ):
            return None
        return [
            {"transaction": json.loads(r["document"]), "merchant_identity": r["merchant_identity"]}
            for r in self.session.run(
                (CYPHER / "neighbourhood.cypher").read_text(),
                query_time=tx["occurred_at"],
                tenant_id=tx["tenant_id"],
                card_id=tx["card_id"],
                merchant_id=tx["merchant_id"],
                merchant_identity=merchant["identity"],
            )
        ]

    def candidates(self, tx):
        from reckoner.v1.evidence.neo4j import Neo4jEvidence

        return [
            r["transaction_id"]
            for r in Neo4jEvidence(self.driver).resolved_cases({"transaction": tx})
        ]

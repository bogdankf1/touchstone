"""Hand-written temporal Cypher and task-owned community GDS projections."""

import json
import time
import uuid
from datetime import timedelta
from pathlib import Path

from reckoner.contracts import content_id
from reckoner.v1.data.history import instant
from reckoner.v1.evidence.assemble import document, neighbourhood, query_transaction

CYPHER = Path(__file__).parent / "cypher"
PARAMETERS = {
    "concurrency": 1,
    "louvain": {
        "maxLevels": 10,
        "maxIterations": 10,
        "tolerance": 0.0001,
        "includeIntermediateCommunities": False,
        "seed_supported": False,
    },
    "page_rank": {"dampingFactor": 0.85, "maxIterations": 20, "tolerance": 1e-7},
}


def import_graph(driver, records, identities, coverage):
    shared = {(r["tenant_id"], r["merchant_id"]): r["identity"] for r in identities}
    with driver.session() as session:
        for statement in (CYPHER / "constraints.cypher").read_text().split(";"):
            if statement.strip():
                session.run(statement).consume()
        rows = []
        for item in records:
            tx, resolution = item["transaction"], item["resolution"]
            rows.append(
                {
                    **{
                        k: tx[k]
                        for k in (
                            "tenant_id",
                            "transaction_id",
                            "occurred_at",
                            "account_id",
                            "card_id",
                            "merchant_id",
                        )
                    },
                    **{
                        f"{kind}_key": content_id([tx["tenant_id"], kind, tx[f"{kind}_id"]])
                        for kind in ("account", "card", "merchant")
                    },
                    "merchant_identity": shared.get((tx["tenant_id"], tx["merchant_id"])),
                    "document": json.dumps(tx, sort_keys=True),
                    "resolution": json.dumps(resolution, sort_keys=True),
                    "resolved_at": resolution["resolved_at"],
                }
            )
        rows.sort(key=lambda row: (row["tenant_id"], row["transaction_id"]))

        def write_records(transaction):
            conflict = transaction.run(
                "UNWIND $rows AS row MATCH(t:Transaction {tenant_id:row.tenant_id,"
                "transaction_id:row.transaction_id}) WHERE t.document<>row.document "
                "OR t.resolution<>row.resolution RETURN t.transaction_id LIMIT 1",
                rows=rows,
            ).single()
            if conflict is not None:
                raise ValueError("graph canonical record conflict")
            transaction.run((CYPHER / "import.cypher").read_text(), rows=rows).consume()
            transaction.run(
                "UNWIND $keys AS key MATCH(a:Merchant {key:key}) MATCH(b:Merchant) "
                "WHERE a.shared_identity=b.shared_identity AND a.tenant_id<>b.tenant_id "
                "WITH DISTINCT CASE WHEN a.tenant_id<b.tenant_id THEN a ELSE b END AS left,"
                "CASE WHEN a.tenant_id<b.tenant_id THEN b ELSE a END AS right "
                "ORDER BY left.key,right.key MERGE(left)-[r:SHARED_IDENTITY]->(right) "
                "SET "
                "r.tenant_id=left.tenant_id,r.other_tenant_id=right.tenant_id,r.occurred_at=CASE "
                "WHEN left.observed_at>right.observed_at THEN left.observed_at ELSE "
                "right.observed_at END",
                keys=sorted({r["merchant_key"] for r in rows}),
            ).consume()

        if rows:
            session.execute_write(write_records)
        for c in coverage:
            session.run(
                "MERGE (n:EvidenceCoverage {tenant_id:$tenant_id}) "
                "ON CREATE SET "
                "n.history_from=datetime($history_from),n.history_until=datetime($history_until),"
                "n.previous_card_complete=$previous_card_complete,n.source_snapshot_id=$source_snapshot_id",
                **c,
            ).consume()


class Neo4jEvidence:
    def __init__(self, driver):
        self.driver = driver

    def project(self, cutoff, tenants):
        boundary = instant(cutoff)
        if boundary.hour or boundary.minute or boundary.second or boundary.microsecond:
            raise ValueError("projection cutoff requires preceding UTC day boundary")
        started = time.perf_counter()
        name = "reckoner-task3-" + uuid.uuid4().hex
        with self.driver.session() as session:
            version = session.run("RETURN gds.version() AS version").single()["version"]
            created = False
            try:
                counts = session.run(
                    (CYPHER / "project.cypher").read_text(),
                    name=name,
                    cutoff=cutoff,
                    tenants=tenants,
                ).single()
                created = True
                louvain = {k: v for k, v in PARAMETERS["louvain"].items() if k != "seed_supported"}
                louvain.update(
                    concurrency=1, relationshipWeightProperty="weight", mutateProperty="community"
                )
                communities = session.run(
                    "CALL gds.louvain.mutate($name,$algorithm) "
                    "YIELD communityCount RETURN communityCount",
                    name=name,
                    algorithm=louvain,
                ).single()
                page = {
                    **PARAMETERS["page_rank"],
                    "concurrency": 1,
                    "relationshipWeightProperty": "weight",
                    "mutateProperty": "pageRank",
                }
                rank = session.run(
                    "CALL gds.pageRank.mutate($name,$algorithm) "
                    "YIELD didConverge,ranIterations RETURN didConverge,ranIterations",
                    name=name,
                    algorithm=page,
                ).single()
                entities = session.run(
                    "MATCH (n:Entity) WHERE n.tenant_id IN $tenants "
                    "AND n.observed_at < datetime($cutoff) RETURN count(CASE WHEN n:Account "
                    "THEN 1 END) AS accounts,"
                    "count(CASE WHEN n:Card THEN 1 END) AS cards,count(CASE WHEN n:Merchant "
                    "THEN 1 END) AS merchants",
                    tenants=tenants,
                    cutoff=cutoff,
                ).single()
                receipt = {
                    "cutoff": cutoff,
                    "window_days": 30,
                    "algorithm": "louvain-page-rank-v1",
                    "gds_version": version,
                    "parameters": PARAMETERS,
                    "node_count": counts["nodeCount"],
                    "edge_count": counts["relationshipCount"],
                    "covered_accounts": entities["accounts"],
                    "covered_cards": entities["cards"],
                    "covered_merchants": entities["merchants"],
                    "build_seconds": time.perf_counter() - started,
                    "page_rank_converged": rank["didConverge"],
                    "page_rank_iterations": rank["ranIterations"],
                    "community_count": communities["communityCount"],
                    "cross_tenant": len(tenants) > 1,
                }
                projection_id = content_id(receipt)
                receipt["projection_id"] = projection_id
                for tenant in tenants:
                    session.run(
                        "CREATE (p:ProjectionReceipt {tenant_id:$tenant,projection_id:$id,"
                        "cutoff:datetime($cutoff),document:$document})",
                        tenant=tenant,
                        id=projection_id,
                        cutoff=cutoff,
                        document=json.dumps(receipt, sort_keys=True),
                    ).consume()
                session.run(
                    "CALL gds.graph.nodeProperties.stream($name,['community','pageRank']) "
                    "YIELD nodeId,nodeProperty,propertyValue WITH gds.util.asNode(nodeId) AS n,"
                    "nodeProperty,propertyValue MERGE (m:GDSMetric "
                    "{tenant_id:n.tenant_id,entity_key:n.key,"
                    "projection_id:$id}) SET m[nodeProperty]=propertyValue",
                    name=name,
                    id=projection_id,
                ).consume()
                return receipt
            finally:
                if created:
                    session.run(
                        "CALL gds.graph.drop($name) YIELD graphName RETURN graphName", name=name
                    ).consume()

    def resolved_cases(self, task: dict) -> list[dict]:
        tx = query_transaction(task)
        with self.driver.session() as session:
            rows = session.run(
                "MATCH(t:Transaction {tenant_id:$tenant}) "
                "WHERE t.occurred_at < datetime($query_at) AND t.resolved_at < datetime($query_at) "
                "AND t.resolved_at >= datetime($query_at)-duration({days:90}) "
                "RETURN t.document AS transaction,t.resolution AS resolution "
                "ORDER BY t.transaction_id",
                tenant=tx["tenant_id"],
                query_at=tx["occurred_at"],
            )
            return [
                json.loads(row["resolution"])
                for row in rows
                if json.loads(row["transaction"])["amount_minor"] > 0
            ]

    def for_task(self, task: dict, config: dict) -> dict:
        tx = query_transaction(task)
        query = instant(tx["occurred_at"])
        with self.driver.session() as session:
            covered = session.run(
                "MATCH (c:EvidenceCoverage {tenant_id:$tenant}) RETURN c", tenant=tx["tenant_id"]
            ).single()
            if (
                covered is None
                or instant(str(covered["c"]["history_from"])) > query - timedelta(days=97)
                or instant(str(covered["c"]["history_until"])) < query
            ):
                return document(
                    tx,
                    "0" * 64,
                    {"status": "unavailable", "missing": ["graph historical coverage unavailable"]},
                    {},
                )
            coverage = covered["c"]
            merchant = session.run(
                "MATCH (m:Merchant {tenant_id:$tenant,identity:$merchant}) "
                "RETURN m.shared_identity AS identity",
                tenant=tx["tenant_id"],
                merchant=tx["merchant_id"],
            ).single()
            rows = session.run(
                (CYPHER / "neighbourhood.cypher").read_text(),
                query_time=tx["occurred_at"],
                tenant_id=tx["tenant_id"],
                card_id=tx["card_id"],
                merchant_id=tx["merchant_id"],
                merchant_identity=merchant["identity"] if merchant else None,
            )
            display = neighbourhood(
                [
                    {
                        "transaction": json.loads(row["document"]),
                        "merchant_identity": row["merchant_identity"],
                    }
                    for row in rows
                ]
            )
            receipt = session.run(
                "MATCH (p:ProjectionReceipt {tenant_id:$tenant}) "
                "WHERE p.cutoff <= datetime($boundary) RETURN p.document AS document "
                "ORDER BY p.cutoff DESC,p.projection_id LIMIT 1",
                tenant=tx["tenant_id"],
                boundary=query.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            ).single()
            missing, features, projection = [], {}, None
            if receipt:
                projection = json.loads(receipt["document"])
                projection["snapshot_age_seconds"] = str(
                    (query - instant(projection["cutoff"])).total_seconds()
                )
                metric = session.run(
                    "MATCH (m:GDSMetric {tenant_id:$tenant,entity_key:$key,projection_id:$id}) "
                    "RETURN m.community AS community,m.pageRank AS rank",
                    tenant=tx["tenant_id"],
                    key=content_id([tx["tenant_id"], "card", tx["card_id"]]),
                    id=projection["projection_id"],
                ).single()
                if metric:
                    features = {
                        "gds_community": str(int(metric["community"])),
                        "gds_page_rank": str(metric["rank"]),
                    }
                else:
                    missing.append("query card absent from GDS projection")
            else:
                missing.append("GDS projection unavailable before query")
            return document(
                tx,
                coverage["source_snapshot_id"],
                {"status": "partial" if missing else "available", "missing": missing},
                features,
                display=display,
                projection=projection,
            )

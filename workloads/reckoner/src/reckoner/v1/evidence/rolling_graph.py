"""Neo4j rolling working set: seeded static structure, rolling transactions, daily GDS.

Ruling R1: entity nodes, card ownership and cross-tenant shared-merchant links are seeded
once from the full source-backed entity manifest, each carrying its first observation.
`project.cypher` admits only entities and edges first observed strictly before its cutoff,
so a projection depends on the seeded structure plus transactions in [cutoff - 30 days,
cutoff) and is identical whatever the rolling schedule. Only Transaction nodes (and their
USED/PAID edges) roll. The store must be empty or already carry this preparation's
EvidenceStore marker; smoke/test markers and any other data are refused before writes.
"""

import json
from collections import Counter, defaultdict
from datetime import timedelta
from time import perf_counter

from reckoner.contracts import content_id
from reckoner.v1.data.history import instant
from reckoner.v1.evidence.neo4j import CYPHER, Neo4jEvidence
from reckoner.v1.evidence.preparation import TENANTS, day_start, iso, projection_action, window

DAY = timedelta(days=1)
BATCH = 5000
FOREIGN_MARKERS = "MATCH (m) WHERE m:SmokeStore OR m:DisposableStore RETURN count(m) AS n"
OWN_MARKERS = (
    "MATCH (m:EvidenceStore) RETURN count(m) AS n, "
    "collect(m.preparation_id + ':' + m.purpose) AS ids"
)
TIME_INDEX = (
    "CREATE INDEX evidence_transaction_time IF NOT EXISTS FOR (t:Transaction) ON (t.occurred_at)"
)
LABELS = {"account": "Account", "card": "Card", "merchant": "Merchant"}
# Lookups go through the unique Entity.key constraint: a per-row label scan over 124,930
# merchants never finishes at archive scale.
SEED_OWNS = (
    "UNWIND $rows AS e MATCH (a:Entity {key:e.account_key}) WHERE a:Account "
    "MATCH (c:Entity {key:e.key}) WHERE c:Card MERGE (a)-[r:OWNS]->(c) "
    "SET r.tenant_id=e.tenant_id, r.occurred_at=datetime(e.first_observed_at)"
)
SEED_SHARED = (
    "UNWIND $rows AS p MATCH (l:Entity {key:p.left}) WHERE l:Merchant "
    "MATCH (r:Entity {key:p.right}) WHERE r:Merchant MERGE (l)-[s:SHARED_IDENTITY]->(r) "
    "SET s.tenant_id=l.tenant_id, s.other_tenant_id=r.tenant_id, "
    "s.occurred_at=datetime(p.occurred_at)"
)


def graph_rows(pairs, identities: dict) -> list[dict]:
    """Rows for `import.cypher`, keyed exactly like the seeded entity nodes."""
    rows = []
    for tx, resolution in pairs:
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
                    for kind in LABELS
                },
                "merchant_identity": identities.get((tx["tenant_id"], tx["merchant_id"])),
                "document": json.dumps(tx, sort_keys=True),
                "resolution": json.dumps(resolution, sort_keys=True),
                "resolved_at": resolution["resolved_at"],
            }
        )
    rows.sort(key=lambda row: (row["tenant_id"], row["transaction_id"]))
    return rows


def reference_summary(transactions, cutoff: str) -> dict:
    """Edges a bounded import of canonical rows implies at `cutoff` (no graph needed)."""
    boundary = instant(cutoff)
    owns, weights = set(), Counter()
    for tx in transactions:
        when = instant(tx["occurred_at"])
        if when >= boundary:
            continue
        keys = {k: content_id([tx["tenant_id"], k, tx[f"{k}_id"]]) for k in LABELS}
        owns.add((keys["account"], keys["card"]))
        if when >= boundary - timedelta(days=30):
            weights[(keys["card"], keys["merchant"])] += 1
    edges = sorted([card, merchant, count] for (card, merchant), count in weights.items())
    return {
        "owns": len(owns),
        "card_merchant_edges": len(edges),
        "card_merchant_weight_total": sum(weights.values()),
        "card_merchant_id": content_id(edges),
    }


class RollingGraph:
    def __init__(
        self,
        driver,
        *,
        preparation_id,
        snapshot_id,
        tenants=TENANTS,
        purpose="evidence",
        guard=None,
    ):
        self.driver = driver
        self.preparation_id, self.snapshot_id = preparation_id, snapshot_id
        self.tenants, self.purpose = list(tenants), purpose
        self.guard = guard or (lambda connection=None: {})
        self.identities = {}

    # -- ownership and seed ------------------------------------------------------------

    def claim(self) -> str:
        with self.driver.session() as session:
            if session.run(FOREIGN_MARKERS).single()["n"]:
                raise ValueError("refusing a store carrying a smoke or disposable test marker")
            owned = session.run(OWN_MARKERS).single()
            if owned["n"]:
                if list(owned["ids"]) != [f"{self.preparation_id}:{self.purpose}"]:
                    raise ValueError("refusing a store owned by another preparation or purpose")
                return "owned"
            if session.run("MATCH (n) RETURN count(n) AS n").single()["n"]:
                raise ValueError("refusing a non-empty graph without this preparation's marker")
            session.run(
                "CREATE (:EvidenceStore {preparation_id:$id, purpose:$purpose, "
                "source_snapshot_id:$snapshot})",
                id=self.preparation_id,
                purpose=self.purpose,
                snapshot=self.snapshot_id,
            ).consume()
        return "claimed"

    def seed(self, manifest: dict) -> dict:
        """Static structure from the full-history manifest, once per store."""
        self.claim()
        merchants = [e for e in manifest["entities"] if e["kind"] == "merchant"]
        self.identities = {(e["tenant_id"], e["identity"]): e["shared_identity"] for e in merchants}
        with self.driver.session() as session:
            seeded = session.run(
                "MATCH (m:EvidenceStore {preparation_id:$id}) RETURN m.seeded AS seeded",
                id=self.preparation_id,
            ).single()["seeded"]
            if seeded == manifest["entity_manifest_id"]:
                return {"seeded": False}
            if seeded is not None:
                raise ValueError("store was seeded from a different entity manifest")
            started = perf_counter()
            for statement in (CYPHER / "constraints.cypher").read_text().split(";"):
                if statement.strip():
                    session.run(statement).consume()
            session.run(TIME_INDEX).consume()
            for kind, label in LABELS.items():
                members = [e for e in manifest["entities"] if e["kind"] == kind]
                for offset in range(0, len(members), BATCH):
                    session.run(
                        f"UNWIND $rows AS e MERGE (n:Entity:{label} {{key:e.key}}) "
                        "SET n.tenant_id=e.tenant_id, n.identity=e.identity, "
                        "n.observed_at=datetime(e.first_observed_at)"
                        + (", n.shared_identity=e.shared_identity" if kind == "merchant" else ""),
                        rows=members[offset : offset + BATCH],
                    ).consume()
            cards = [
                {
                    **e,
                    "account_key": content_id([e["tenant_id"], "account", e["account_identity"]]),
                }
                for e in manifest["entities"]
                if e["kind"] == "card"
            ]
            for offset in range(0, len(cards), BATCH):
                session.run(
                    SEED_OWNS,
                    rows=cards[offset : offset + BATCH],
                ).consume()
            for offset in range(0, len(merchants), BATCH):
                session.run(
                    "UNWIND $rows AS e MERGE (m:MerchantIdentity "
                    "{tenant_id:e.tenant_id, merchant_id:e.identity}) "
                    "SET m.shared_identity=e.shared_identity",
                    rows=merchants[offset : offset + BATCH],
                ).consume()
            pairs = []
            groups = defaultdict(list)
            for e in merchants:
                groups[e["shared_identity"]].append(e)
            for members in groups.values():
                for left in members:
                    for right in members:
                        if left["tenant_id"] < right["tenant_id"]:
                            pairs.append(
                                {
                                    "left": left["key"],
                                    "right": right["key"],
                                    "occurred_at": max(
                                        left["first_observed_at"], right["first_observed_at"]
                                    ),
                                }
                            )
            for offset in range(0, len(pairs), BATCH):
                session.run(
                    SEED_SHARED,
                    rows=pairs[offset : offset + BATCH],
                ).consume()
            session.run(
                "MATCH (m:EvidenceStore {preparation_id:$id}) SET m.seeded=$manifest",
                id=self.preparation_id,
                manifest=manifest["entity_manifest_id"],
            ).consume()
        return {
            "seeded": True,
            "entities": len(manifest["entities"]),
            "owns": len(cards),
            "shared_identity_links": len(pairs),
            "merchant_declarations": len(merchants),
            "seconds": perf_counter() - started,
        }

    # -- rolling transactions -----------------------------------------------------------

    def coverage(self) -> dict:
        with self.driver.session() as session:
            return {
                row["tenant"]: {"history_from": row["start"], "history_until": row["end"]}
                for row in session.run(
                    "MATCH (c:EvidenceCoverage) RETURN c.tenant_id AS tenant, "
                    "c.history_from AS start, c.history_until AS end"
                )
            }

    def import_day(self, start, pairs) -> dict:
        """One transaction: Transaction nodes for the day, then raise `history_until`."""
        rows = graph_rows(pairs, self.identities)
        end = start + DAY
        keys = sorted({row[f"{k}_key"] for row in rows for k in LABELS})
        query = (CYPHER / "import.cypher").read_text()

        def write(transaction):
            unseeded = transaction.run(
                "UNWIND $keys AS k OPTIONAL MATCH (n:Entity {key:k}) WITH k, n WHERE n IS NULL "
                "RETURN count(k) AS n",
                keys=keys,
            ).single()["n"]
            if unseeded:
                raise ValueError("transaction references an entity missing from the seed")
            conflict = transaction.run(
                "UNWIND $rows AS row MATCH (t:Transaction {tenant_id:row.tenant_id,"
                "transaction_id:row.transaction_id}) WHERE t.document<>row.document "
                "OR t.resolution<>row.resolution RETURN t.transaction_id LIMIT 1",
                rows=rows,
            ).single()
            if conflict is not None:
                raise ValueError("graph canonical record conflict")
            if rows:
                transaction.run(query, rows=rows).consume()
            for tenant in self.tenants:
                current = transaction.run(
                    "MERGE (c:EvidenceCoverage {tenant_id:$tenant}) "
                    "ON CREATE SET c.history_from=datetime($start), "
                    "c.history_until=datetime($start) "
                    "RETURN c.history_until = datetime($start) AS contiguous",
                    tenant=tenant,
                    start=iso(start),
                ).single()["contiguous"]
                if not current:
                    raise ValueError(f"graph coverage is not contiguous at {iso(start)}")
                transaction.run(
                    "MATCH (c:EvidenceCoverage {tenant_id:$tenant}) "
                    "SET c.history_until=datetime($end), c.previous_card_complete=true, "
                    "c.source_snapshot_id=$snapshot",
                    tenant=tenant,
                    end=iso(end),
                    snapshot=self.snapshot_id,
                ).consume()
            return self.guard(None)

        started = perf_counter()
        with self.driver.session() as session:
            guard = session.execute_write(write)
        return {
            "day": iso(start),
            "rows": len(rows),
            "seconds": perf_counter() - started,
            "guard": guard,
        }

    def advance_to(self, day: str, source) -> list[dict]:
        target = day_start(day) + DAY
        coverage = self.coverage()
        if coverage:
            untils = {str(row["history_until"]) for row in coverage.values()}
            if set(coverage) != set(self.tenants) or len(untils) != 1:
                raise ValueError("graph coverage differs between tenants")
            cursor = instant(untils.pop())
        else:
            cursor = window(day)[0]
        imported = []
        while cursor < target:
            started = perf_counter()
            pairs = source.records(cursor, cursor + DAY)
            read = perf_counter() - started
            imported.append({**self.import_day(cursor, pairs), "read_seconds": read})
            cursor += DAY
        return imported

    def evict_before(self, boundary) -> dict:
        """Raise `history_from` first, then delete older Transaction nodes (USED/PAID only)."""
        started = perf_counter()
        with self.driver.session() as session:
            moved = session.run(
                "MATCH (c:EvidenceCoverage) WHERE c.tenant_id IN $tenants "
                "AND c.history_from <= datetime($boundary) "
                "AND c.history_until > datetime($boundary) "
                "SET c.history_from = datetime($boundary) RETURN count(c) AS n",
                tenants=self.tenants,
                boundary=iso(boundary),
            ).single()["n"]
            if moved != len(self.tenants):
                raise ValueError(f"cannot evict before {iso(boundary)}: coverage not monotonic")
            deleted = 0
            while True:
                batch = session.run(
                    "MATCH (t:Transaction) WHERE t.occurred_at < datetime($boundary) "
                    "WITH t LIMIT $limit DETACH DELETE t RETURN count(*) AS n",
                    boundary=iso(boundary),
                    limit=BATCH,
                ).single()["n"]
                deleted += batch
                if batch < BATCH:
                    break
        return {"boundary": iso(boundary), "rows": deleted, "seconds": perf_counter() - started}

    # -- daily projections -----------------------------------------------------------

    def _receipts(self, session, cutoff):
        return [
            {**json.loads(row["document"]), "tenant_id": row["tenant"]}
            for row in session.run(
                "MATCH (p:ProjectionReceipt) WHERE p.cutoff = datetime($cutoff) "
                "RETURN p.tenant_id AS tenant, p.document AS document",
                cutoff=cutoff,
            )
        ]

    def _metric_count(self, session, projection_id):
        return session.run(
            "MATCH (m:GDSMetric {projection_id:$id}) RETURN count(m) AS n", id=projection_id
        ).single()["n"]

    def _delete_metrics(self, session, keep=()):
        removed = 0
        while True:
            batch = session.run(
                "MATCH (m:GDSMetric) WHERE NOT m.projection_id IN $keep "
                "WITH m LIMIT $limit DELETE m RETURN count(*) AS n",
                keep=list(keep),
                limit=BATCH * 4,
            ).single()["n"]
            removed += batch
            if batch < BATCH * 4:
                return removed

    def projection_for(self, day: str, *, referenced) -> dict:
        """Build, reuse or rebuild the cutoff-D projection; never reuse a partial one in use."""
        cutoff = iso(day_start(day))
        with self.driver.session() as session:
            receipts = self._receipts(session, cutoff)
            ids = sorted({r["projection_id"] for r in receipts})
            self._delete_metrics(session, keep=ids)
            metrics = self._metric_count(session, ids[0]) if len(ids) == 1 else 0
            action = projection_action(
                receipts, self.tenants, metrics, referenced=any(referenced(i) for i in ids)
            )
            if action == "refuse":
                raise ValueError(f"inconsistent projection receipts for {cutoff} are referenced")
            if action == "reuse":
                receipt = {k: v for k, v in receipts[0].items() if k != "tenant_id"}
                return {**receipt, "action": "reuse"}
            if action == "rebuild":
                session.run(
                    "MATCH (p:ProjectionReceipt) WHERE p.cutoff = datetime($cutoff) "
                    "DETACH DELETE p",
                    cutoff=cutoff,
                ).consume()
                self._delete_metrics(session)
        receipt = Neo4jEvidence(self.driver).project(cutoff, self.tenants)
        return {**receipt, "action": action}

    def release(self, projection_id: str) -> int:
        """Delete one day's GDSMetric nodes; the receipt nodes are kept."""
        with self.driver.session() as session:
            removed = 0
            while True:
                batch = session.run(
                    "MATCH (m:GDSMetric {projection_id:$id}) WITH m LIMIT $limit DELETE m "
                    "RETURN count(*) AS n",
                    id=projection_id,
                    limit=BATCH * 4,
                ).single()["n"]
                removed += batch
                if batch < BATCH * 4:
                    return removed

    def edge_summary(self, cutoff: str) -> dict:
        """The projection's inputs at `cutoff`, with the same filters as `project.cypher`."""
        with self.driver.session() as session:
            nodes = {
                row["kind"]: row["n"]
                for row in session.run(
                    "MATCH (n:Entity) WHERE n.tenant_id IN $tenants "
                    "AND n.observed_at < datetime($cutoff) "
                    "RETURN CASE WHEN n:Account THEN 'account' WHEN n:Card THEN 'card' "
                    "ELSE 'merchant' END AS kind, count(n) AS n",
                    tenants=self.tenants,
                    cutoff=cutoff,
                )
            }
            owns = session.run(
                "MATCH (a:Account)-[r:OWNS]->(:Card) WHERE a.tenant_id IN $tenants "
                "AND r.occurred_at < datetime($cutoff) RETURN count(r) AS n",
                tenants=self.tenants,
                cutoff=cutoff,
            ).single()["n"]
            shared = session.run(
                "MATCH (a:Merchant)-[r:SHARED_IDENTITY]->(b:Merchant) "
                "WHERE a.tenant_id IN $tenants "
                "AND b.tenant_id IN $tenants AND r.occurred_at < datetime($cutoff) "
                "RETURN count(r) AS n",
                tenants=self.tenants,
                cutoff=cutoff,
            ).single()["n"]
            edges = sorted(
                [row["card"], row["merchant"], row["weight"]]
                for row in session.run(
                    "MATCH (c:Card)-[:USED]->(t:Transaction)-[:PAID]->(m:Merchant) "
                    "WHERE c.tenant_id IN $tenants AND t.occurred_at < datetime($cutoff) "
                    "AND t.occurred_at >= datetime($cutoff)-duration({days:30}) "
                    "RETURN c.key AS card, m.key AS merchant, count(t) AS weight",
                    tenants=self.tenants,
                    cutoff=cutoff,
                )
            )
        return {
            "cutoff": cutoff,
            "nodes": dict(sorted(nodes.items())),
            "node_count": sum(nodes.values()),
            "owns": owns,
            "shared_identity": shared,
            "card_merchant_edges": len(edges),
            "card_merchant_weight_total": sum(edge[2] for edge in edges),
            "card_merchant_id": content_id(edges),
            "directed_edge_count": 2 * (owns + shared + len(edges)),
        }


def gds_documents(neo4j, runner_dsn, manifests, transactions, config) -> list[dict]:
    """GDS-augmented documents over the persisted relational base (run via `isolated`)."""
    from neo4j import GraphDatabase

    from reckoner.v1.evidence.assemble import assemble_evidence
    from reckoner.v1.evidence.rolling import PersistedRelational

    uri, user, password = neo4j
    driver = GraphDatabase.driver(uri, auth=(user, password), warn_notification_severity="OFF")
    with driver:
        base, graph = PersistedRelational(runner_dsn, manifests), Neo4jEvidence(driver)
        return [assemble_evidence({"transaction": tx}, config, base, graph) for tx in transactions]

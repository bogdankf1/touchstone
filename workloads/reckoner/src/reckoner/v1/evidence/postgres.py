"""Postgres neighbourhood baseline and exact structured-feature pgvector search."""

from datetime import timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from reckoner.v1.contracts import validate_v1
from reckoner.v1.data.history import instant
from reckoner.v1.evidence.assemble import document, neighbourhood, query_transaction
from reckoner.v1.evidence.features import feature_vector, history_features
from reckoner.v1.evidence.indicators import risk_indicators


def import_evidence(dsn, queries, records, identities, coverage, scaler, include_vectors=True):
    """Owner preparation requires complete source history for every supported candidate."""
    transactions = [row["transaction"] for row in records]
    with psycopg.connect(dsn) as connection:
        for tx in queries + transactions:
            connection.execute(
                "INSERT INTO reckoner.transactions VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (tx["tenant_id"], tx["transaction_id"], Jsonb(tx)),
            )
            stored = connection.execute(
                "SELECT document FROM reckoner.transactions WHERE "
                "tenant_id=%s AND transaction_id=%s",
                (tx["tenant_id"], tx["transaction_id"]),
            ).fetchone()[0]
            if stored != tx:
                raise ValueError("canonical record conflict")
        for item in identities:
            connection.execute(
                "INSERT INTO reckoner.v1_merchant_identities VALUES(%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (item["tenant_id"], item["merchant_id"], item["identity"]),
            )
        for c in coverage:
            connection.execute(
                "INSERT INTO reckoner.v1_evidence_coverage VALUES(%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                tuple(
                    c[k]
                    for k in (
                        "tenant_id",
                        "history_from",
                        "history_until",
                        "previous_card_complete",
                        "source_snapshot_id",
                    )
                ),
            )
        for row in records:
            tx = row["transaction"]
            connection.execute(
                "INSERT INTO reckoner.v1_history VALUES(%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                tuple(
                    tx[k]
                    for k in (
                        "tenant_id",
                        "transaction_id",
                        "occurred_at",
                        "account_id",
                        "card_id",
                        "merchant_id",
                    )
                )
                + (Jsonb(tx),),
            )
            resolution = validate_v1("resolution", row["resolution"])
            connection.execute(
                "INSERT INTO oracle.v1_resolutions VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                tuple(
                    resolution[k]
                    for k in (
                        "tenant_id",
                        "transaction_id",
                        "resolution_policy_version",
                        "resolved_at",
                    )
                )
                + (Jsonb(resolution),),
            )
            if include_vectors and tx["amount_minor"] > 0:
                if "history" not in row:
                    raise ValueError(
                        "complete candidate history required before vector preparation"
                    )
                vector = feature_vector(tx, row["history"], scaler)
                connection.execute(
                    "INSERT INTO reckoner.v1_vectors VALUES(%s,%s,%s,%s,%s::vector) "
                    "ON CONFLICT DO NOTHING",
                    (
                        tx["tenant_id"],
                        tx["transaction_id"],
                        scaler["scaler_id"],
                        "features-v1",
                        str(vector),
                    ),
                )


class PostgresEvidence:
    def __init__(self, dsn):
        self.dsn = dsn

    def resolved_cases(self, task: dict) -> list[dict]:
        tx = query_transaction(task)
        with psycopg.connect(self.dsn) as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT document FROM reckoner.v1_comparable_candidates(%s,%s)",
                    (tx["tenant_id"], tx["transaction_id"]),
                ).fetchall()
            ]

    def for_task(self, task: dict, config: dict) -> dict:
        tx = query_transaction(task)
        key = (tx["tenant_id"], tx["transaction_id"])
        query = instant(tx["occurred_at"])
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            stored = connection.execute(
                "SELECT document FROM reckoner.transactions "
                "WHERE tenant_id=%s AND transaction_id=%s",
                key,
            ).fetchone()
            if stored is None or stored["document"] != tx:
                raise ValueError("canonical query transaction mismatch")
            coverage = connection.execute(
                "SELECT * FROM reckoner.v1_runtime_coverage WHERE tenant_id=%s", (tx["tenant_id"],)
            ).fetchone()
            missing = []
            if (
                coverage is None
                or coverage["history_from"] > query - timedelta(days=97)
                or coverage["history_until"] < query
                or not coverage["previous_card_complete"]
            ):
                return document(
                    tx,
                    "0" * 64,
                    {"status": "unavailable", "missing": ["historical coverage unavailable"]},
                    {"amount_usd": str(tx["amount_minor"] / 100), "prior_24h_count": None},
                )
            rows = connection.execute(
                "SELECT * FROM reckoner.v1_history_before(%s,%s)", key
            ).fetchall()
            previous = connection.execute(
                "SELECT * FROM reckoner.v1_previous_card(%s,%s)", key
            ).fetchone()
            history = {
                "status": "available",
                "transactions": [r["document"] for r in rows],
                "previous": previous["document"] if previous else None,
            }
            values = history_features(tx, history)
            neighbours = connection.execute(
                "SELECT * FROM reckoner.v1_neighbours(%s,%s)", key
            ).fetchall()
            display = neighbourhood(
                [
                    {"transaction": r["document"], "merchant_identity": r["merchant_identity"]}
                    for r in neighbours
                ]
            )
            resolved = connection.execute(
                "SELECT * FROM reckoner.v1_merchant_resolved(%s,%s)", key
            ).fetchall()
            merchant = {
                "status": "available",
                "resolved_count": len(resolved),
                "fraud_count": sum(r["document"]["verdict"] == "decline" for r in resolved),
                "evidence_refs": sorted(r["document"]["resolution_id"] for r in resolved),
            }
            vector = feature_vector(tx, history, config["scaler"])
            if config["scaler_id"] != config["scaler"]["scaler_id"]:
                raise ValueError("pinned scaler mismatch")
            complete = connection.execute(
                "SELECT reckoner.v1_vector_coverage(%s,%s,%s) AS complete",
                (*key, config["scaler_id"]),
            ).fetchone()["complete"]
            if not complete:
                missing.append("eligible comparable vectors unavailable")
            cases = (
                connection.execute(
                    "SELECT * FROM reckoner.v1_vector_search(%s,%s,%s::vector,%s)",
                    (*key, str(vector), config["scaler_id"]),
                ).fetchall()
                if complete
                else []
            )
            comparables = [
                {
                    **{
                        k: r["document"][k]
                        for k in (
                            "tenant_id",
                            "transaction_id",
                            "verdict",
                            "resolved_at",
                            "resolution_policy_version",
                        )
                    },
                    "similarity": str(1 / (1 + r["distance"])),
                    "evidence_refs": [r["document"]["resolution_id"]],
                }
                for r in cases
            ]
            values["excluded_unsupported_comparables"] = connection.execute(
                "SELECT reckoner.v1_comparable_exclusions(%s,%s) AS n", key
            ).fetchone()["n"]
            features = {
                k: str(v) if v is not None else None
                for k, v in values.items()
                if k not in {"evidence_refs", "status"}
            }
            return document(
                tx,
                coverage["source_snapshot_id"],
                {"status": "partial" if missing else "available", "missing": missing},
                features,
                risk_indicators(tx, values, merchant),
                comparables,
                display,
            )

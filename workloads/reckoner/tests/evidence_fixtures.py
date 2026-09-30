"""Fabricated simulated histories with deliberately unordered source records."""

import os
import uuid

import pytest
from reckoner.v1.data.history import historical_resolution
from test_v1_history import transaction


def records(tenants=("tenant-a", "tenant-b")):
    query = {**transaction(), "transaction_id": "query", "occurred_at": "2018-06-01T12:00:00Z"}
    rows = []
    for identity, when, tenant in [
        ("z", "2018-05-20T12:00:00Z", "tenant-a"),
        ("a", "2018-05-20T12:00:00Z", "tenant-a"),
        ("b", "2018-05-20T12:00:00Z", "tenant-a"),
        ("c", "2018-05-20T12:00:00Z", "tenant-a"),
        ("d", "2018-05-20T12:00:00Z", "tenant-a"),
        ("e", "2018-05-20T12:00:00Z", "tenant-a"),
        ("cross", "2018-05-21T12:00:00Z", "tenant-b"),
        ("unresolved", "2018-05-25T12:00:00Z", "tenant-a"),
        ("equal", query["occurred_at"], "tenant-a"),
        ("future", "2018-06-02T12:00:00Z", "tenant-a"),
        ("old", "2018-01-01T12:00:00Z", "tenant-a"),
    ]:
        tx = {**query, "transaction_id": identity, "occurred_at": when, "tenant_id": tenant}
        if tenant == "tenant-b":
            tx.update(
                account_id="cross-account", card_id="cross-card", merchant_id="cross-merchant"
            )
        rows.append(
            {
                "transaction": tx,
                "resolution": historical_resolution(
                    tx, "fraud" if identity == "cross" else "legitimate", "simulated-seven-days-v1"
                ),
            }
        )
    for item in rows:
        tx = item["transaction"]
        previous = [
            r["transaction"]
            for r in rows
            if r["transaction"]["tenant_id"] == tx["tenant_id"]
            and r["transaction"]["card_id"] == tx["card_id"]
            and r["transaction"]["occurred_at"] < tx["occurred_at"]
        ]
        item["history"] = {
            "status": "available",
            "transactions": previous,
            "previous": max(
                previous, key=lambda r: (r["occurred_at"], r["transaction_id"]), default=None
            ),
        }
    identities = [
        {"tenant_id": "tenant-a", "merchant_id": query["merchant_id"], "identity": "shared"},
        {"tenant_id": "tenant-b", "merchant_id": "cross-merchant", "identity": "shared"},
    ]
    names = dict(zip(("tenant-a", "tenant-b"), tenants, strict=True))
    query["tenant_id"] = names[query["tenant_id"]]
    for item in rows:
        old = item["transaction"]["tenant_id"]
        item["transaction"]["tenant_id"] = names[old]
        item["resolution"] = historical_resolution(
            item["transaction"],
            "fraud" if item["transaction"]["transaction_id"] == "cross" else "legitimate",
            "simulated-seven-days-v1",
        )
    for item in identities:
        item["tenant_id"] = names[item["tenant_id"]]
    return query, rows, identities


def coverage(tenants=("tenant-a", "tenant-b")):
    return [
        {
            "tenant_id": t,
            "history_from": "2017-01-01T00:00:00Z",
            "history_until": "2019-01-01T00:00:00Z",
            "previous_card_complete": True,
            "source_snapshot_id": "a" * 64,
        }
        for t in tenants
    ]


@pytest.fixture
def graph_driver():
    from neo4j import GraphDatabase

    uri = os.environ.get("RECKONER_TEST_NEO4J_URI")
    if not uri:
        pytest.fail("neo4j integration requires RECKONER_TEST_NEO4J_URI")
    driver = GraphDatabase.driver(uri, auth=("neo4j", "reckoner-test-only"))
    driver.verify_connectivity()
    suffix = uuid.uuid4().hex
    driver.test_tenants = (f"test-{suffix}-a", f"test-{suffix}-b")
    yield driver
    with driver.session() as session:
        session.run(
            "MATCH(n) WHERE n.tenant_id IN $tenants DETACH DELETE n",
            tenants=list(driver.test_tenants),
        ).consume()
    driver.close()

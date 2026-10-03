"""Fixed fabricated Reckoner v1 deployment smoke, confined to a disposable smoke database.

All data here is a tiny fabricated simulated source generated deterministically in code;
its content identity is the immutable source that the graph is reimported from. No
provider is ever called. With the graph service stopped, evidence is partial and the
workflow records a degraded escalation without dispatch. Graph-available evidence is
persisted for inspection only: scoring it needs a separately approved paid protocol.
"""

import json

from reckoner.contracts import content_id
from reckoner.data.adapter import adapt_row
from reckoner.resources import CONFIG
from reckoner.smoke import is_smoke_database
from reckoner.v1.data.history import historical_resolution

RUN_ID = "fabricated-smoke-v1"
TENANTS = ("tenant-a", "tenant-b")
POLICY = "simulated-seven-days-v1"
PROJECTION_CUTOFF = "2018-06-01T00:00:00Z"
CREATED_AT = "2026-10-03T00:00:00Z"
# (tenant, day in May 2018, merchant name, amount, label); every resolution precedes June 1.
HISTORY = [
    *[
        ("tenant-a", day, ("5001", "5002")[day % 2], f"${10 + day}.25", "No")
        for day in range(3, 21)
    ],
    ("tenant-a", 21, "5001", "$480.00", "Yes"),
    ("tenant-a", 22, "5002", "$12.00", "Yes"),
    *[
        ("tenant-b", day, ("5001", "5003")[day % 2], f"${30 + day}.50", "No")
        for day in range(4, 20)
    ],
    ("tenant-b", 20, "5001", "$350.00", "Yes"),
]
QUERIES = [
    ("tenant-a", "10:00", "5001", "$95.00"),
    ("tenant-a", "11:30", "5002", "$18.40"),
    ("tenant-b", "09:15", "5001", "$220.00"),
    ("tenant-b", "13:45", "5003", "$41.10"),
]


def _source_row(tenant, year, month, day, time, merchant, amount, label):
    return {
        "User": "smoke-" + tenant,
        "Card": "0",
        "Year": str(year),
        "Month": str(month),
        "Day": str(day),
        "Time": time,
        "Amount": amount,
        "Use Chip": "Swipe Transaction",
        "Merchant Name": merchant,
        "Merchant City": "Fabricated",
        "Merchant State": "ZZ",
        "Zip": "00000",
        "MCC": "5411",
        "Errors?": "",
        "Is Fraud?": label,
    }


def fabricated_source() -> dict:
    """Deterministic simulated records, queries, scope declarations and coverage."""
    rows = [
        (tenant, _source_row(tenant, 2018, 5, day, "12:00", merchant, amount, label))
        for tenant, day, merchant, amount, label in HISTORY
    ]
    rows += [
        (tenant, _source_row(tenant, 2018, 6, 1, time, merchant, amount, "No"))
        for tenant, time, merchant, amount in QUERIES
    ]
    source_id = content_id({"fabricated_smoke_source": [[t, r] for t, r in rows]})
    adapted, names = [], {}
    for ordinal, (tenant, row) in enumerate(rows, start=1):
        result = adapt_row(row, source_sha256=source_id, source_record=ordinal, tenant_id=tenant)
        if result["transaction"] is None:
            raise ValueError("fabricated smoke row is invalid: " + result["reason"])
        adapted.append((result["transaction"], row["Is Fraud?"] == "Yes"))
        names[(tenant, result["transaction"]["merchant_id"])] = row["Merchant Name"]
    history = adapted[: len(HISTORY)]
    queries = [tx for tx, _ in adapted[len(HISTORY) :]]
    records = []
    for tx, fraud in history:
        previous = sorted(
            (
                other
                for other, _ in history
                if other["tenant_id"] == tx["tenant_id"]
                and other["card_id"] == tx["card_id"]
                and other["occurred_at"] < tx["occurred_at"]
            ),
            key=lambda other: (other["occurred_at"], other["transaction_id"]),
        )
        records.append(
            {
                "transaction": tx,
                "resolution": historical_resolution(tx, "fraud" if fraud else "legitimate", POLICY),
                "history": {
                    "status": "available",
                    "transactions": previous,
                    "previous": previous[-1] if previous else None,
                },
            }
        )
    # Merchant IDs are tenant-scoped hashes; the declared identity links the same source
    # merchant name across tenants (only merchant 5001 occurs in both).
    identities = [
        {"tenant_id": tenant, "merchant_id": merchant, "identity": "smoke-merchant-" + name}
        for (tenant, merchant), name in sorted(names.items())
    ]
    coverage = [
        {
            "tenant_id": tenant,
            "history_from": "2018-01-01T00:00:00Z",
            "history_until": "2018-07-01T00:00:00Z",
            "previous_card_complete": True,
            "source_snapshot_id": source_id,
        }
        for tenant in TENANTS
    ]
    return {
        "source_id": source_id,
        "queries": queries,
        "records": records,
        "identities": identities,
        "coverage": coverage,
    }


def smoke_scaler() -> dict:
    from reckoner.v1.evidence.features import fit_scaler

    observations = []
    for ordinal, amount in enumerate(("$4.00", "$25.00", "$90.00", "$400.00"), start=1):
        row = _source_row("tenant-a", 2017, 12, ordinal, "08:00", "5002", amount, "No")
        tx = adapt_row(row, source_sha256="0" * 64, source_record=ordinal, tenant_id="tenant-a")
        observations.append(
            {
                "transaction": tx["transaction"],
                "history": {"status": "available", "transactions": [], "previous": None},
                "weight": 1,
                "purpose": "development",
            }
        )
    return fit_scaler(observations)


def _price(name):
    return json.loads((CONFIG / name).read_text())


def smoke_config(tenant: str, scaler_id: str) -> dict:
    threshold = json.loads((CONFIG / f"thresholds-{tenant}-v1.json").read_text())
    haiku = "anthropic/claude-haiku-4-5-20251001"
    body = {
        "schema_version": "reckoner-run-config-v1",
        "tenant_id": tenant,
        "workflow_version": "reckoner-v1",
        "scorer": {
            "provider": "typesafe",
            "model": "jev-1.13.0",
            "question_version": "binary-v1",
            "price_table": _price("jev-prices-v1.json"),
        },
        "note_model": {
            "provider": "anthropic",
            "model": haiku,
            "prompt_version": "note-v1",
            "price_table": _price("anthropic-prices-v1.json"),
        },
        "judge_model": {
            "provider": "anthropic",
            "model": haiku,
            "prompt_version": "judge-v1",
            "price_table": _price("anthropic-prices-v1.json"),
        },
        "threshold_config_id": threshold["config_id"],
        "feature_version": "features-v1",
        "scaler_id": scaler_id,
        "calibration_id": None,
        "score_mode": "raw",
        "graph_version": "graph-v1",
        "retrieval_version": "retrieval-v1",
        "resolution_policy_version": POLICY,
        "limits": {
            "timeout_seconds": 30,
            "input_token_ceiling": 2000,
            "max_output_tokens": 1000,
            "maximum_attempts": 1,
        },
    }
    return {**body, "config_id": content_id(body)}


def smoke_manifest(tenant: str, config: dict, source: dict) -> dict:
    tasks = [
        {"task_id": f"smoke-task-{index}", "transaction_id": tx["transaction_id"]}
        for index, tx in enumerate(source["queries"])
        if tx["tenant_id"] == tenant
    ]
    body = {
        "schema_version": "reckoner-experiment-v1",
        "tenant_id": tenant,
        "run_id": RUN_ID,
        "purpose": "fabricated",
        "config_id": config["config_id"],
        "dataset_simulated": True,
        "dataset_version": source["source_id"],
        "cohort_version": content_id(tasks),
        "code_revision": "fabricated-smoke",
        "created_at": CREATED_AT,
        "tasks": tasks,
    }
    return {**body, "experiment_id": content_id(body)}


def _require_smoke(dsn: str) -> None:
    if not is_smoke_database(dsn):
        raise ValueError("v1 smoke requires a dedicated reckoner_smoke_ database")


def seed_postgres(owner_dsn: str) -> dict:
    """Owner import of the fabricated source, threshold and run configuration, and runs."""
    from reckoner.storage.postgres import PostgresRepository
    from reckoner.v1.evidence.postgres import import_evidence
    from reckoner.v1.storage.repository import V1Repository

    _require_smoke(owner_dsn)
    source, scaler = fabricated_source(), smoke_scaler()
    with V1Repository(owner_dsn) as repo:
        foreign = repo._connection.execute(
            "SELECT 1 FROM reckoner.v1_runs WHERE run_id <> %s LIMIT 1", (RUN_ID,)
        ).fetchone()
        if foreign:
            raise ValueError("smoke database contains another experiment")
    import_evidence(
        owner_dsn,
        source["queries"],
        source["records"],
        source["identities"],
        source["coverage"],
        scaler,
    )
    with PostgresRepository(owner_dsn) as baseline:
        for tenant in TENANTS:
            baseline.register_threshold_config(
                json.loads((CONFIG / f"thresholds-{tenant}-v1.json").read_text())
            )
    tasks = 0
    with V1Repository(owner_dsn) as repo:
        for tenant in TENANTS:
            config = smoke_config(tenant, scaler["scaler_id"])
            repo.register_config(config)
            manifest = smoke_manifest(tenant, config, source)
            repo.create_run(manifest, config["config_id"])
            tasks += len(manifest["tasks"])
    return {
        "run_id": RUN_ID,
        "source_id": source["source_id"],
        "scaler_id": scaler["scaler_id"],
        "history_records": len(source["records"]),
        "tasks": tasks,
        "provider_calls": 0,
    }


def _foreign_graph_nodes(driver) -> int:
    with driver.session() as session:
        return session.run(
            "MATCH (n) WHERE n.tenant_id IS NOT NULL AND NOT n.tenant_id IN $tenants "
            "RETURN count(n) AS n",
            tenants=list(TENANTS),
        ).single()["n"]


def seed_graph(driver) -> dict:
    """Import the immutable fabricated source and build one declared GDS projection."""
    from reckoner.v1.evidence.neo4j import Neo4jEvidence, import_graph

    if _foreign_graph_nodes(driver):
        raise ValueError("graph contains non-smoke tenants; refusing the smoke import")
    source = fabricated_source()
    import_graph(driver, source["records"], source["identities"], source["coverage"])
    receipt = Neo4jEvidence(driver).project(PROJECTION_CUTOFF, list(TENANTS))
    return {"source_id": source["source_id"], "projection": receipt, "provider_calls": 0}


def _tasks(repo):
    return [
        repo.task(row["tenant_id"], row["run_id"], row["task_id"])
        for row in repo._connection.execute(
            "SELECT tenant_id, run_id, task_id FROM reckoner.v1_tasks WHERE run_id=%s "
            "ORDER BY tenant_id, task_id",
            (RUN_ID,),
        ).fetchall()
    ]


def _private_config(repo, task, scaler):
    from reckoner.v1.workflow.runner import configuration

    return {**configuration(repo, task), "scaler": scaler}


def graph_evidence(runner_dsn: str, driver) -> dict:
    """Persist graph-augmented evidence for every task; never scores it (no protocol)."""
    from reckoner.v1.evidence.assemble import assemble_evidence
    from reckoner.v1.evidence.neo4j import Neo4jEvidence
    from reckoner.v1.evidence.postgres import PostgresEvidence
    from reckoner.v1.storage.repository import V1Repository

    _require_smoke(runner_dsn)
    scaler, results = smoke_scaler(), []
    relational, graph = PostgresEvidence(runner_dsn), Neo4jEvidence(driver)
    with V1Repository(runner_dsn) as repo:
        for task in _tasks(repo):
            document = assemble_evidence(
                {"transaction": task["transaction"]},
                _private_config(repo, task, scaler),
                relational,
                graph,
            )
            repo.persist_evidence(document)
            results.append(_evidence_summary(task, document))
    return {"evidence": results, "workflow_executed": False, "provider_calls": 0}


def _evidence_summary(task, document):
    projection = document.get("graph_projection")
    return {
        "tenant_id": task["tenant_id"],
        "task_id": task["task_id"],
        "evidence_id": document["evidence_id"],
        "coverage": document["coverage"],
        "graph_snapshot": document["source_snapshot_ids"]["graph"],
        "page_rank_converged": projection["page_rank_converged"] if projection else None,
        "neighbourhood_transactions": (document.get("neighbourhood") or {}).get(
            "total_transactions"
        ),
    }


def run_without_graph(runner_dsn: str) -> dict:
    """Graph service stopped: partial evidence routes every task to a degraded escalation."""
    from reckoner.v1.evidence.assemble import assemble_evidence
    from reckoner.v1.evidence.postgres import PostgresEvidence
    from reckoner.v1.storage.checkpoints import PostgresCheckpointer
    from reckoner.v1.storage.repository import V1Repository
    from reckoner.v1.workflow import build_graph, run_task

    _require_smoke(runner_dsn)
    scaler, decisions = smoke_scaler(), []
    relational = PostgresEvidence(runner_dsn)
    with V1Repository(runner_dsn) as repo:
        for task in _tasks(repo):
            document = assemble_evidence(
                {"transaction": task["transaction"]},
                _private_config(repo, task, scaler),
                relational,
                None,
            )
            if document["coverage"]["status"] == "available":
                raise ValueError("graph-free smoke evidence unexpectedly complete")
            repo.persist_evidence(document)
            settings = {
                "evidence_id": document["evidence_id"],
                "evidence_mode": "gds-augmented",
                "data_kind": "fabricated",
                "protocol": None,
                "calibration": None,
            }
            with PostgresCheckpointer(runner_dsn, task["tenant_id"]) as saver:
                decision = run_task(repo, build_graph(saver), task, settings)
            decisions.append(
                {
                    "tenant_id": decision["tenant_id"],
                    "task_id": decision["task_id"],
                    "outcome": decision["outcome"],
                    "scorer_status": decision["scorer_status"],
                    "degraded_reason": decision["degraded_reason"],
                    "missing": document["coverage"]["missing"],
                }
            )
    return {"run_id": RUN_ID, "decisions": decisions, "provider_calls": 0}

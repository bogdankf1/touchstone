"""One source-backed bounded resource receipt; simulated data, zero providers.

Run against the task-owned Compose stores after focused integration tests.
Full histories stay in the existing immutable bundle. This is not a full
operational import, model comparison, or validation-performance evaluation.
"""

import csv
import json
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path

import psycopg
from neo4j import GraphDatabase
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from reckoner.contracts import content_id
from reckoner.storage.migrate import migrate
from reckoner.v1.data.history import POLICY, SourceHistory, historical_resolution, instant
from reckoner.v1.evidence.features import fit_scaler
from reckoner.v1.evidence.neo4j import Neo4jEvidence, import_graph
from reckoner.v1.evidence.postgres import PostgresEvidence

ROOT = Path(__file__).resolve().parents[3]
BUNDLE = ROOT / "artifacts/phase3/data/frozen-v1"
SOURCE = Path("/Users/bohdanburukhin/Projects/personal/touchstone/archive")
OUTPUT = ROOT / "artifacts/phase3/task3"
CUTOFF = "2018-06-01T00:00:00Z"
END = "2018-06-02T00:00:00Z"
START = (instant(CUTOFF) - timedelta(days=97)).isoformat().replace("+00:00", "Z")
DSN = "postgresql://postgres:reckoner-test-only@127.0.0.1:55433/task3_benchmark"
MAX_DERIVED = 20 * 1024**3
MIN_FREE = 15 * 1024**3


def resource_guard(connection):
    size = connection.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    derived = sum(p.stat().st_size for p in BUNDLE.iterdir() if p.is_file())
    derived += sum(p.stat().st_size for p in OUTPUT.rglob("*") if p.is_file())
    if shutil.disk_usage(ROOT).free < MIN_FREE or derived + size > MAX_DERIVED:
        raise ValueError("bounded benchmark disk guard exceeded")
    return size


def main():
    started = time.perf_counter()
    stop_samples = threading.Event()
    samples = []

    def sample_resources():
        with (OUTPUT / "service-samples.jsonl").open("w") as output:
            while not stop_samples.is_set():
                result = subprocess.run(
                    [
                        "docker",
                        "stats",
                        "--no-stream",
                        "--format",
                        "{{json .}}",
                        "touchstone-phase3-task3-neo4j-1",
                        "touchstone-phase3-task3-postgres-1",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                for line in result.stdout.splitlines():
                    row = {"elapsed_seconds": time.perf_counter() - started, **json.loads(line)}
                    samples.append(row)
                    output.write(json.dumps(row) + "\n")
                    output.flush()
                stop_samples.wait(5)

    threading.Thread(target=sample_resources, daemon=True).start()
    index = json.loads((BUNDLE / "bundle.json").read_text())
    queries = [
        json.loads(line)
        for line in (BUNDLE / "runtime_validation.jsonl").read_text().splitlines()
        if CUTOFF <= json.loads(line)["occurred_at"] < END
    ]
    if len(queries) != 7:
        raise ValueError("frozen June1 query scope changed")
    with psycopg.connect(
        "postgresql://postgres:reckoner-test-only@127.0.0.1:55433/postgres", autocommit=True
    ) as admin:
        admin.execute("CREATE DATABASE task3_benchmark")
    migrate(DSN)
    driver = GraphDatabase.driver(
        "bolt://127.0.0.1:57687",
        auth=("neo4j", "reckoner-test-only"),
        warn_notification_severity="OFF",
    )
    coverage = [
        {
            "tenant_id": t,
            "history_from": START,
            "history_until": END,
            "previous_card_complete": True,
            "source_snapshot_id": index["history_id"],
        }
        for t in ("tenant-a", "tenant-b")
    ]
    with (
        SourceHistory(BUNDLE, SOURCE) as history,
        sqlite3.connect(f"file:{BUNDLE}/resolutions.sqlite?mode=ro", uri=True) as labels,
        psycopg.connect(DSN) as connection,
    ):
        initial_size = resource_guard(connection)
        initial_free = shutil.disk_usage(ROOT).free
        manifest_path = OUTPUT / "entity-manifest.json"
        if manifest_path.exists():
            cached = json.loads(manifest_path.read_text())
            if cached["bundle_id"] != index["bundle_id"]:
                raise ValueError("cached entity manifest source mismatch")
            manifest = cached["entities"]
        else:
            manifest = [
                dict(
                    zip(
                        ("key", "tenant_id", "kind", "identity", "occurred_at", "source_record"),
                        r,
                        strict=True,
                    )
                )
                for r in history.connection.execute(
                    "SELECT "
                    "e.entity_key,e.tenant_id,e.kind,e.identity,h.occurred_at,h.source_record "
                    "FROM entities e JOIN (SELECT account_key AS entity_key,min(occurred_at) AS "
                    "occurred_at,source_record "
                    "FROM history GROUP BY account_key UNION ALL SELECT "
                    "card_key,min(occurred_at),source_record "
                    "FROM history GROUP BY card_key UNION ALL SELECT "
                    "merchant_key,min(occurred_at),source_record "
                    "FROM history GROUP BY merchant_key) h USING(entity_key)"
                )
            ]
        if sum(e["kind"] == "account" for e in manifest) != 1441:
            raise ValueError("full user scope changed")
        merchant_identities = {}
        with driver.session() as session:
            preserved = list(
                session.run(
                    "MATCH(m:Merchant) WHERE m.tenant_id IN $tenants "
                    "RETURN m.tenant_id AS tenant,m.identity AS identity,m.shared_identity "
                    "AS shared",
                    tenants=["tenant-a", "tenant-b"],
                )
            )
        if len(preserved) == sum(e["kind"] == "merchant" for e in manifest):
            merchant_identities = {(r["tenant"], r["identity"]): r["shared"] for r in preserved}
        else:
            source_path = SOURCE / index["transaction_source"]
            with source_path.open("rb") as stream:
                headers = next(csv.reader([stream.readline().decode("utf-8-sig")]))
                for entity in manifest:
                    if entity["kind"] == "merchant":
                        offset = history.connection.execute(
                            "SELECT source_offset FROM history WHERE source_record=?",
                            (entity["source_record"],),
                        ).fetchone()[0]
                        stream.seek(offset)
                        raw = dict(
                            zip(
                                headers, next(csv.reader([stream.readline().decode()])), strict=True
                            )
                        )
                        merchant_identities[(entity["tenant_id"], entity["identity"])] = content_id(
                            {
                                "dataset": "cctd",
                                "entity": "shared-merchant",
                                "source_merchant": raw["Merchant Name"],
                            }
                        )
        identities = [
            {"tenant_id": t, "merchant_id": m, "identity": identity}
            for (t, m), identity in merchant_identities.items()
        ]
        (OUTPUT / "entity-manifest.json").write_text(
            json.dumps({"bundle_id": index["bundle_id"], "entities": manifest}, sort_keys=True)
        )
        print(
            json.dumps({"stage": "manifest", "entities": len(manifest), "accounts": 1441}),
            flush=True,
        )
        scaler_path = OUTPUT / "resource-scaler.json"
        if scaler_path.exists():
            scaler = json.loads(scaler_path.read_text())
            if scaler["scaler_id"] != content_id(
                {k: v for k, v in scaler.items() if k != "scaler_id"}
            ):
                raise ValueError("frozen scaler identity mismatch")
            print(json.dumps({"stage": "scaler", "observations": 2000, "reused": True}), flush=True)
        else:
            # Complete source card history for the frozen development scaler; no outcome labels.
            sample = json.loads((BUNDLE / "manifest_development.json").read_text())
            print(
                json.dumps({"stage": "development_scaler", "manifest_keys": list(sample)}),
                flush=True,
            )
            weights = (
                {s["label"]: s["weight"] for s in sample["strata"]}
                if isinstance(sample["strata"], list)
                else {label: stratum["weight"] for label, stratum in sample["strata"].items()}
            )
            # Runtime files contain all frozen development members and no labels.
            # Weights come from privileged selection manifests, never vector coordinates.
            # by privileged selection manifests, never as runtime vector coordinates.
            development = []
            development_labels = {
                json.loads(line)["transaction_id"]: json.loads(line)["label"]
                for line in (BUNDLE / "oracle_development.jsonl").read_text().splitlines()
            }
            for line in (BUNDLE / "runtime_development.jsonl").read_text().splitlines():
                tx = json.loads(line)
                rows = list(
                    history.card_before(
                        tx["tenant_id"],
                        tx["card_id"],
                        tx["occurred_at"],
                        since=(instant(tx["occurred_at"]) - timedelta(days=30)).isoformat(),
                    )
                )
                previous = history.previous_card(tx["tenant_id"], tx["card_id"], tx["occurred_at"])
                development.append(
                    {
                        "purpose": "development",
                        "weight": weights[development_labels[tx["transaction_id"]]],
                        "transaction": tx,
                        "history": {
                            "status": "available",
                            "transactions": rows,
                            "previous": previous,
                        },
                    }
                )
            scaler = fit_scaler(development)
            (OUTPUT / "resource-scaler.json").write_text(json.dumps(scaler, sort_keys=True))
            print(json.dumps({"stage": "scaler", "observations": len(development)}), flush=True)
        # All manifest entities remain represented; future first observations remain
        # excluded by the projection, and original histories are unchanged on disk.
        import_graph(driver, [], identities, coverage)
        with driver.session() as session:
            entity_count = session.run(
                "MATCH(n:Entity) WHERE n.tenant_id IN $tenants RETURN count(n) AS n",
                tenants=["tenant-a", "tenant-b"],
            ).single()["n"]
        if entity_count != len(manifest):
            with driver.session() as session:
                for begin in range(0, len(manifest), 2000):
                    entities = manifest[begin : begin + 2000]
                    rows = [
                        {
                            "key": content_id([e["tenant_id"], e["kind"], e["identity"]]),
                            "tenant_id": e["tenant_id"],
                            "identity": e["identity"],
                            "kind": e["kind"],
                            "occurred_at": e["occurred_at"],
                            "shared_identity": merchant_identities.get(
                                (e["tenant_id"], e["identity"])
                            ),
                        }
                        for e in entities
                    ]
                    for kind in ("account", "card", "merchant"):
                        session.run(
                            f"UNWIND $rows AS row MERGE(n:Entity:{kind.title()} {{key:row.key}}) "
                            "SET "
                            ""
                            "n.tenant_id=row.tenant_id,n.identity=row.identity,n.observed_at=datetime(row.occurred_at),"
                            "n.shared_identity=row.shared_identity",
                            rows=[r for r in rows if r["kind"] == kind],
                        ).consume()
        for tx in queries:
            connection.execute(
                "INSERT INTO reckoner.transactions VALUES(%s,%s,%s)",
                (tx["tenant_id"], tx["transaction_id"], Jsonb(tx)),
            )
        for c in coverage:
            connection.execute(
                "INSERT INTO reckoner.v1_evidence_coverage VALUES(%s,%s,%s,%s,%s)",
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
        with connection.cursor().copy("COPY reckoner.v1_merchant_identities FROM STDIN") as copier:
            for r in identities:
                copier.write_row((r["tenant_id"], r["merchant_id"], r["identity"]))
        # Freeze complete working-set row pointers; add full-history previous query-card rows.
        history.connection.execute(
            "CREATE TEMP TABLE benchmark_rows(source_record INTEGER PRIMARY KEY)"
        )
        history.connection.execute(
            "INSERT INTO benchmark_rows SELECT source_record FROM history "
            "WHERE occurred_at>=? AND occurred_at<?",
            (START, END),
        )
        for tx in queries:
            previous = history.previous_card(tx["tenant_id"], tx["card_id"], tx["occurred_at"])
            if previous:
                history.connection.execute(
                    "INSERT OR IGNORE INTO benchmark_rows VALUES(?)",
                    (previous["provenance"]["source_record"],),
                )
        count = history.connection.execute("SELECT count(*) FROM benchmark_rows").fetchone()[0]
        print(json.dumps({"stage": "import", "expected": count}), flush=True)
        batch, imported = [], 0
        extraction = time.perf_counter()
        path = OUTPUT / "source-records.jsonl"
        with driver.session() as session:
            existing_graph = {
                (r["tenant"], r["identity"])
                for r in session.run(
                    "MATCH(t:Transaction) WHERE t.tenant_id IN $tenants "
                    "RETURN t.tenant_id AS tenant,t.transaction_id AS identity",
                    tenants=["tenant-a", "tenant-b"],
                )
            }
        cached_count, last_record = 0, 0
        if path.exists():
            with path.open() as cached:
                for line in cached:
                    item = json.loads(line)
                    ordinal = item["transaction"]["provenance"]["source_record"]
                    if ordinal <= last_record:
                        raise ValueError("cached source order changed")
                    last_record = ordinal
                    batch.append(item)
                    cached_count += 1
                    if len(batch) == 2000:
                        load_batch(connection, driver, batch, identities, queries, existing_graph)
                        imported += len(batch)
                        batch.clear()
                        resource_guard(connection)
                        if imported % 20000 == 0:
                            print(
                                json.dumps(
                                    {
                                        "stage": "resume-copy",
                                        "rows": imported,
                                        "seconds": time.perf_counter() - extraction,
                                    }
                                ),
                                flush=True,
                            )
        with path.open("a") as output:
            for (record,) in history.connection.execute(
                "SELECT source_record FROM benchmark_rows WHERE source_record>? ORDER "
                "BY source_record",
                (last_record,),
            ):
                tx = history.transaction(record)
                label = labels.execute(
                    "SELECT label FROM resolutions WHERE source_record=?", (record,)
                ).fetchone()[0]
                item = {"transaction": tx, "resolution": historical_resolution(tx, label, POLICY)}
                output.write(json.dumps(item, sort_keys=True) + "\n")
                batch.append(item)
                if len(batch) == 2000:
                    load_batch(connection, driver, batch, identities, queries, existing_graph)
                    imported += len(batch)
                    batch.clear()
                    resource_guard(connection)
            if batch:
                load_batch(connection, driver, batch, identities, queries, existing_graph)
                imported += len(batch)
        if imported != count:
            raise ValueError("complete bounded import row count mismatch")
        connection.commit()
        import_seconds = time.perf_counter() - extraction
        db_size = resource_guard(connection)
        print(
            json.dumps({"stage": "projection", "imported": imported, "database_bytes": db_size}),
            flush=True,
        )
        graph = Neo4jEvidence(driver)
        projection = graph.project(CUTOFF, ["tenant-a", "tenant-b"])
        runner_dsn = make_conninfo(DSN, options="-c role=reckoner_runner")
        relational = PostgresEvidence(runner_dsn)
        reconciled = []
        config = {"scaler": scaler, "scaler_id": scaler["scaler_id"]}
        for tx in queries:
            task = {"transaction": tx, "query_time": tx["occurred_at"]}
            t = time.perf_counter()
            sql = relational.for_task(task, config)
            sql_seconds = time.perf_counter() - t
            t = time.perf_counter()
            cypher = graph.for_task(task, config)
            cypher_seconds = time.perf_counter() - t
            if sql["neighbourhood"] != cypher["neighbourhood"]:
                raise ValueError("real source SQL/Cypher sets differ")
            reconciled.append(
                {
                    "tenant_id": tx["tenant_id"],
                    "transaction_id": tx["transaction_id"],
                    "query_time": tx["occurred_at"],
                    "sql_seconds": sql_seconds,
                    "cypher_seconds": cypher_seconds,
                    "neighbourhood": sql["neighbourhood"],
                    "sql_coverage": sql["coverage"],
                    "graph_coverage": cypher["coverage"],
                    "graph_projection": cypher.get("graph_projection"),
                }
            )
        receipt = {
            "dataset_simulated": True,
            "provider_calls": 0,
            "bundle_id": index["bundle_id"],
            "entity_manifest_id": content_id(manifest),
            "scope_accounts": 1441,
            "scope_entities": len(manifest),
            "source_histories_retained": index["history"]["retained_records"],
            "working_history_from": START,
            "working_history_until": END,
            "imported_records": imported,
            "import_mode": "resumed-from-checkpoints" if cached_count else "cold",
            "cached_source_records": cached_count,
            "preserved_graph_records": len(existing_graph),
            "import_seconds": import_seconds,
            "database_initial_bytes": initial_size,
            "database_bytes": db_size,
            "host_free_initial_bytes": initial_free,
            "host_free_final_bytes": shutil.disk_usage(ROOT).free,
            "projection": projection,
            "query_results": reconciled,
            "query_count": len(queries),
            "uncovered_query_count": sum(
                r["graph_coverage"]["status"] != "available" for r in reconciled
            ),
            "duration_seconds": time.perf_counter() - started,
            "limitations": [
                "One bounded snapshot only; full operational import and all-query coverage "
                "unproven.",
                "Resource run omits historical candidate vectors; their absence is explicit "
                "in SQL coverage.",
                "No model utility, fraud-ring discovery, or validation-performance claim.",
            ],
        }
        stop_samples.set()
        receipt["service_memory_samples"] = samples
        receipt["neo4j_data_kib"] = subprocess.run(
            ["docker", "exec", "touchstone-phase3-task3-neo4j-1", "du", "-sk", "/data"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        receipt["receipt_id"] = content_id(receipt)
        (OUTPUT / "resource-receipt.json").write_text(json.dumps(receipt, indent=2))
        print(
            json.dumps(
                {
                    "stage": "receipt",
                    "receipt_id": receipt["receipt_id"],
                    "uncovered": receipt["uncovered_query_count"],
                }
            ),
            flush=True,
        )
    driver.close()


def load_batch(connection, driver, batch, identities, queries, existing_graph):
    query_ids = {q["transaction_id"] for q in queries}
    with connection.cursor().copy("COPY reckoner.transactions FROM STDIN") as copier:
        for item in batch:
            tx = item["transaction"]
            if tx["transaction_id"] not in query_ids:
                copier.write_row((tx["tenant_id"], tx["transaction_id"], Jsonb(tx)))
    with connection.cursor().copy("COPY reckoner.v1_history FROM STDIN") as copier:
        for item in batch:
            tx = item["transaction"]
            copier.write_row(
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
                + (Jsonb(tx),)
            )
    with connection.cursor().copy("COPY oracle.v1_resolutions FROM STDIN") as copier:
        for item in batch:
            r = item["resolution"]
            copier.write_row(
                tuple(
                    r[k]
                    for k in (
                        "tenant_id",
                        "transaction_id",
                        "resolution_policy_version",
                        "resolved_at",
                    )
                )
                + (Jsonb(r),)
            )
    missing = [
        row
        for row in batch
        if (row["transaction"]["tenant_id"], row["transaction"]["transaction_id"])
        not in existing_graph
    ]
    if missing:
        import_graph(driver, missing, identities, [])


if __name__ == "__main__":
    main()

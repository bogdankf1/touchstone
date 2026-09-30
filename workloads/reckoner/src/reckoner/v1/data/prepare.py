"""Checksum-first, bounded preparation; archive remains the full-history authority."""

import csv
import json
import os
import shutil
import sqlite3
import tempfile
from collections import Counter
from datetime import timedelta
from pathlib import Path

from reckoner.contracts import content_id
from reckoner.data.adapter import REQUIRED_COLUMNS, adapt_row
from reckoner.data.artifacts import canonical_json, canonical_jsonl, sha256_file, verify_bundle
from reckoner.data.profile import TRANSACTIONS_FILE
from reckoner.v1.data.history import POLICY, instant
from reckoner.v1.data.sampling import SEED, Selection, selection_key

GIB = 1024**3
MIN_FREE = 15 * GIB
MAX_DERIVED = 20 * GIB


def _resource_check(directory: Path):
    size = sum(path.stat().st_size for path in directory.iterdir() if path.is_file())
    if size > MAX_DERIVED:
        raise ValueError("derived data exceeds 20 GiB cap")
    if shutil.disk_usage(directory).free < MIN_FREE:
        raise ValueError("preparation requires at least 15 GiB free disk")


def _metadata(bundle: Path, index: dict, key: str):
    return json.loads((bundle / index["files"][key]["path"]).read_text())


def _write(path: Path, documents: list[dict]):
    path.write_bytes(canonical_jsonl(documents))
    os.chmod(path, 0o600)
    return {"path": path.name, "sha256": sha256_file(path), "records": len(documents)}


def _publish_samples(directory: Path, selections: dict, source_hash: str):
    development = selections["development"]["selected"]
    pilot = []
    for label, count in [("fraud", 2), ("legitimate", 18)]:
        candidates = [r for r in development if r["oracle"]["label"] == label]
        pilot.extend(
            sorted(
                candidates, key=lambda r: selection_key("pilot", r["transaction"]["transaction_id"])
            )[:count]
        )
    selections["pilot"] = {
        "purpose": "pilot",
        "year": 2017,
        "seed": SEED,
        "selected": sorted(pilot, key=lambda r: r["transaction"]["transaction_id"]),
        "strata": {},
        "exclusions": {},
        "reuse_policy": "only identical frozen request hashes; reuse explicitly identified",
        "parent_sample": "development",
    }
    samples, files = {}, {}
    for purpose, selection in selections.items():
        records = selection["selected"]
        runtime = sorted((r["transaction"] for r in records), key=lambda tx: tx["transaction_id"])
        oracle = sorted((r["oracle"] for r in records), key=lambda tx: tx["transaction_id"])
        for kind, documents in [("runtime", runtime), ("oracle", oracle)]:
            name = f"{kind}_{purpose}"
            files[name] = _write(directory / f"{name}.jsonl", documents)
        body = {key: value for key, value in selection.items() if key != "selected"}
        body.update(
            schema_version="reckoner-sample-v1",
            dataset_simulated=True,
            source_sha256=source_hash,
            resolution_policy_version=POLICY,
            selected_transaction_ids=[tx["transaction_id"] for tx in runtime],
            tenant_counts=dict(Counter(tx["tenant_id"] for tx in runtime)),
            inclusion_method="lowest sha256(seed:purpose:transaction_id) per class",
            runtime_file=f"runtime_{purpose}",
            oracle_file=f"oracle_{purpose}",
        )
        body["sample_id"] = content_id(body)
        samples[purpose] = body
        path = directory / f"manifest_{purpose}.json"
        path.write_bytes(canonical_json(body))
        files[f"manifest_{purpose}"] = {
            "path": path.name,
            "sha256": sha256_file(path),
            "records": 1,
        }
    return samples, files


def prepare_v1(source: Path, baseline_bundle: Path, output: Path) -> dict:
    """Prepare a new immutable bundle without providers or credential access.

    SQLite's disk indexes sort complete histories using a 32 MiB page cache.
    Canonical JSON is materialized only for bounded selected samples. Complete
    archive rows are reached by byte offset and record ordinal; labels are stored
    separately, in preparation-role files. Atomic publication rejects any reused path.
    """
    source, baseline_bundle, output = Path(source), Path(baseline_bundle), Path(output)
    if output.exists():
        raise ValueError("output path already exists")
    if not output.parent.is_dir():
        raise ValueError("output parent directory must exist")
    if shutil.disk_usage(output.parent).free < MIN_FREE:
        raise ValueError("preparation requires at least 15 GiB free disk")
    baseline = verify_bundle(baseline_bundle, source)
    history = _metadata(baseline_bundle, baseline, "history_entities")
    assignments = {
        r["source_user_id"]: r["tenant_id"]
        for r in _metadata(baseline_bundle, baseline, "tenant_assignments")["assignments"]
    }
    retained = {r["source_user_id"] for r in history["users"]}
    pilot_path = (
        baseline_bundle / baseline["files"][baseline["cohorts"]["pilot"]["oracle_file"]]["path"]
    )
    excluded = {json.loads(line)["transaction_id"] for line in pilot_path.read_text().splitlines()}
    selectors = {year: Selection(year, SEED, excluded) for year in (2017, 2018)}
    source_hash = baseline["source_files"][TRANSACTIONS_FILE]["sha256"]
    directory = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    os.chmod(directory, 0o700)
    counts, statuses, invalid = Counter(), Counter(), Counter()
    entities = {}
    try:
        with (
            sqlite3.connect(directory / "history.sqlite") as db,
            sqlite3.connect(directory / "resolutions.sqlite") as resolutions,
        ):
            for connection in (db, resolutions):
                connection.execute("PRAGMA journal_mode=OFF")
                connection.execute("PRAGMA synchronous=OFF")
                connection.execute("PRAGMA temp_store=FILE")
                connection.execute("PRAGMA cache_size=-32768")
            db.execute(
                "CREATE TABLE entities(entity_key INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL, "
                "kind TEXT NOT NULL, identity TEXT NOT NULL, UNIQUE(tenant_id,kind,identity))"
            )
            db.execute(
                "CREATE TABLE history(source_record INTEGER PRIMARY KEY, source_offset INTEGER "
                "NOT NULL, tenant_id TEXT NOT NULL, account_key INTEGER NOT NULL, card_key INTEGER "
                "NOT NULL, merchant_key INTEGER NOT NULL, occurred_at TEXT NOT NULL, "
                "amount_minor INTEGER NOT NULL, payment_channel TEXT NOT NULL)"
            )
            resolutions.execute(
                "CREATE TABLE resolutions(source_record INTEGER PRIMARY KEY, "
                "tenant_id TEXT NOT NULL, label TEXT NOT NULL, resolved_at TEXT NOT NULL)"
            )
            batch, resolution_batch = [], []
            with (source / TRANSACTIONS_FILE).open("rb") as stream:
                reader = csv.DictReader(line.decode("utf-8-sig") for line in stream)
                if REQUIRED_COLUMNS - set(reader.fieldnames or []):
                    raise ValueError("missing required transaction source columns")
                ordinal = 0
                while True:
                    offset = stream.tell()
                    try:
                        row = next(reader)
                    except StopIteration:
                        break
                    ordinal += 1
                    counts["source_records"] += 1
                    user = (row.get("User") or "").strip()
                    if user not in retained:
                        continue
                    counts["retained_records"] += 1
                    result = adapt_row(
                        row,
                        source_sha256=source_hash,
                        source_record=ordinal,
                        tenant_id=assignments[user],
                    )
                    statuses[result["status"]] += 1
                    if result["transaction"] is None:
                        invalid[result["reason"]] += 1
                        continue
                    tx, oracle = result["transaction"], result["oracle"]
                    keys = []
                    for kind in ("account", "card", "merchant"):
                        identity = (tx["tenant_id"], kind, tx[f"{kind}_id"])
                        if identity not in entities:
                            entities[identity] = len(entities) + 1
                            db.execute(
                                "INSERT INTO entities VALUES (?,?,?,?)",
                                (entities[identity], *identity),
                            )
                        keys.append(entities[identity])
                    batch.append(
                        (
                            ordinal,
                            offset,
                            tx["tenant_id"],
                            *keys,
                            tx["occurred_at"],
                            tx["amount_minor"],
                            tx["payment_channel"],
                        )
                    )
                    available = (instant(tx["occurred_at"]) + timedelta(days=7)).isoformat()
                    resolution_batch.append(
                        (
                            ordinal,
                            tx["tenant_id"],
                            oracle["label"],
                            available.replace("+00:00", "Z"),
                        )
                    )
                    if oracle["label"] == "fraud":
                        counts["covered_fraud"] += 1
                    year = instant(tx["occurred_at"]).year
                    if year in selectors and result["status"] == "eligible":
                        selectors[year].add(result)
                    if len(batch) >= 10000:
                        db.executemany("INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?)", batch)
                        resolutions.executemany(
                            "INSERT INTO resolutions VALUES (?,?,?,?)", resolution_batch
                        )
                        db.commit()
                        resolutions.commit()
                        batch.clear()
                        resolution_batch.clear()
                        _resource_check(directory)
            db.executemany("INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?)", batch)
            resolutions.executemany("INSERT INTO resolutions VALUES (?,?,?,?)", resolution_batch)
            db.commit()
            resolutions.commit()
            if invalid:
                raise ValueError(f"strict adapter invalid retained records: {dict(invalid)}")
            if counts["retained_records"] != baseline["history"]["retained_records"] or (
                counts["covered_fraud"] != baseline["history"]["covered_fraud"]
            ):
                raise ValueError(f"source history inventory discrepancy: {dict(counts)}")
            selections = {s.purpose: s.finish() for s in selectors.values()}
            # A planning scan is not a frozen manifest. Stop before publication if
            # the actual strict adapter disagrees with the approved archive inventory.
            if (
                baseline["bundle_id"]
                == "2dcc4a9838db77552872ea489ff8a8e3b13a84b37a5806b2b02291c9f23848e0"
            ):
                expected = {
                    "development": {"fraud": 212, "legitimate": 1506955},
                    "validation": {"fraud": 2367, "legitimate": 1503196},
                }
                actual = {
                    purpose: {label: values["N_h"] for label, values in s["strata"].items()}
                    for purpose, s in selections.items()
                }
                if actual != expected:
                    raise ValueError(
                        f"strict adapter stratum discrepancy: {actual}; expected {expected}"
                    )
            _resource_check(directory)
            db.execute(
                "CREATE INDEX history_card_time ON history(card_key,occurred_at,source_record)"
            )
            db.execute(
                "CREATE INDEX history_account_time "
                "ON history(account_key,occurred_at,source_record)"
            )
            db.execute("CREATE INDEX history_time ON history(occurred_at,source_record)")
            resolutions.execute(
                "CREATE INDEX resolution_time ON resolutions(resolved_at,source_record)"
            )
            db.commit()
            resolutions.commit()
        samples, files = _publish_samples(directory, selections, source_hash)
        for name in ("history", "resolutions"):
            path = directory / f"{name}.sqlite"
            os.chmod(path, 0o600)
            files[name] = {
                "path": path.name,
                "sha256": sha256_file(path),
                "records": counts["retained_records"],
            }
        body = {
            "schema_version": "reckoner-preparation-v1",
            "dataset_simulated": True,
            "seed": SEED,
            "baseline_bundle_id": baseline["bundle_id"],
            "history_id": history["history_id"],
            "source_sha256": source_hash,
            "transaction_source": TRANSACTIONS_FILE,
            "source_files": baseline["source_files"],
            "resolution_policy_version": POLICY,
            "samples": samples,
            "files": files,
            "history": {
                **dict(counts),
                "statuses": dict(statuses),
                "retained_users": len(retained),
                "entity_occurrences": len(entities),
                "complete_source_backed_histories": True,
                "authority": "checksum-pinned immutable source archive",
                "oracle_access": "privileged preparation/import role only",
            },
            "excluded_prior_pilot_ids": sorted(excluded),
        }
        body["bundle_id"] = content_id(body)
        (directory / "bundle.json").write_bytes(canonical_json(body))
        _resource_check(directory)
        # Serialize publishers for this output, then publish the complete directory
        # in one rename. The output never exists in a partially populated state.
        lock = output.parent / f".{output.name}.publication.lock"
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            if output.exists():
                raise ValueError("output path already exists")
            os.rename(directory, output)
        finally:
            os.close(descriptor)
            lock.unlink()
        return body
    finally:
        if directory.exists():
            shutil.rmtree(directory)


def verify_preparation(bundle: Path, source: Path | None = None) -> dict:
    """Verify content identities and every derived checksum before privileged use."""
    bundle = Path(bundle)
    index = json.loads((bundle / "bundle.json").read_text())
    if index["bundle_id"] != content_id({k: v for k, v in index.items() if k != "bundle_id"}):
        raise ValueError("preparation identity mismatch")
    for metadata in index["files"].values():
        relative = Path(metadata["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("preparation path escapes bundle")
        path = (bundle / relative).resolve()
        if not path.is_relative_to(bundle.resolve()) or sha256_file(path) != metadata["sha256"]:
            raise ValueError("preparation artifact checksum mismatch")
    if source is not None:
        for name, metadata in index["source_files"].items():
            if sha256_file(Path(source) / name) != metadata["sha256"]:
                raise ValueError("preparation source checksum mismatch")
    return index


def import_v1(
    bundle: Path, source: Path, owner_dsn: str, baseline_bundle: Path | None = None
) -> dict:
    """Import the union of 90-day query windows plus previous-card records.

    Complete source histories stay indexed on disk. The union is an explicitly
    bounded working set, retaining prior-card state outside those windows. A
    baseline bundle adds the frozen 2019 queries without rerunning any provider.
    The database import is atomic and requires owner privileges.
    """
    import psycopg
    from psycopg.types.json import Jsonb

    from reckoner.v1.data.history import SourceHistory, historical_resolution

    index = verify_preparation(bundle, source)
    queries = {}
    for purpose in ("development", "validation"):
        path = bundle / index["files"][f"runtime_{purpose}"]["path"]
        for line in path.read_text().splitlines():
            tx = json.loads(line)
            queries[tx["transaction_id"]] = tx
    if baseline_bundle is not None:
        baseline = verify_bundle(baseline_bundle, source)
        if baseline["bundle_id"] != index["baseline_bundle_id"]:
            raise ValueError("baseline bundle identity mismatch")
        path = (
            baseline_bundle
            / baseline["files"][baseline["cohorts"]["baseline"]["runtime_file"]]["path"]
        )
        for line in path.read_text().splitlines():
            tx = json.loads(line)
            queries[tx["transaction_id"]] = tx
    # Coalesced intervals avoid one query per source row or a 23M-element ID set.
    intervals = sorted(
        (instant(tx["occurred_at"]) - timedelta(days=97), instant(tx["occurred_at"]))
        for tx in queries.values()
    )
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    with (
        SourceHistory(bundle, source) as history,
        sqlite3.connect(f"file:{bundle / 'resolutions.sqlite'}?mode=ro", uri=True) as resolutions,
        psycopg.connect(owner_dsn) as connection,
    ):
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (732019006,))
        # Check authority before traversing the archive or reading oracle rows.
        if not connection.execute(
            "SELECT has_table_privilege(current_user, 'oracle.v1_resolutions', 'INSERT')"
        ).fetchone()[0]:
            raise psycopg.errors.InsufficientPrivilege("owner import privileges required")
        previous = set()
        for tx in queries.values():
            row = history.connection.execute(
                "SELECT h.source_record FROM history h JOIN entities e ON e.entity_key=h.card_key "
                "WHERE h.tenant_id=? AND e.identity=? AND h.occurred_at<? "
                "ORDER BY h.occurred_at DESC,h.source_record LIMIT 1",
                (tx["tenant_id"], tx["card_id"], tx["occurred_at"]),
            ).fetchone()
            if row:
                previous.add(row[0])
        history.connection.execute("CREATE TEMP TABLE selected(source_record INTEGER PRIMARY KEY)")
        for start, end in merged:
            history.connection.execute(
                "INSERT OR IGNORE INTO selected SELECT source_record FROM history "
                "WHERE occurred_at>=? AND occurred_at<?",
                (start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")),
            )
        history.connection.executemany(
            "INSERT OR IGNORE INTO selected VALUES (?)",
            ((ordinal,) for ordinal in sorted(previous)),
        )

        def insert_transaction(tx):
            key = (tx["tenant_id"], tx["transaction_id"])
            connection.execute(
                "INSERT INTO reckoner.transactions VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (*key, Jsonb(tx)),
            )
            stored = connection.execute(
                "SELECT document FROM reckoner.transactions "
                "WHERE tenant_id=%s AND transaction_id=%s",
                key,
            ).fetchone()[0]
            if stored != tx:
                raise ValueError("historical canonical record conflict")

        for tx in queries.values():
            insert_transaction(tx)
        count = 0
        for (ordinal,) in history.connection.execute(
            "SELECT source_record FROM selected ORDER BY source_record"
        ):
            tx = history.transaction(ordinal)
            oracle = resolutions.execute(
                "SELECT label FROM resolutions WHERE source_record=?", (ordinal,)
            ).fetchone()
            resolution = historical_resolution(tx, oracle[0], POLICY)
            key = (tx["tenant_id"], tx["transaction_id"])
            insert_transaction(tx)
            existing = connection.execute(
                "SELECT document FROM reckoner.v1_history WHERE tenant_id=%s AND transaction_id=%s",
                key,
            ).fetchone()
            if existing is not None:
                if existing[0] != tx:
                    raise ValueError("historical canonical record conflict")
                stored_resolution = connection.execute(
                    "SELECT document FROM oracle.v1_resolutions WHERE tenant_id=%s "
                    "AND transaction_id=%s AND resolution_policy_version=%s",
                    (*key, POLICY),
                ).fetchone()
                if stored_resolution is None or stored_resolution[0] != resolution:
                    raise ValueError("historical resolution record conflict")
                continue
            connection.execute(
                "INSERT INTO reckoner.v1_history VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    *key,
                    tx["occurred_at"],
                    tx["account_id"],
                    tx["card_id"],
                    tx["merchant_id"],
                    Jsonb(tx),
                ),
            )
            connection.execute(
                "INSERT INTO oracle.v1_resolutions VALUES (%s,%s,%s,%s,%s)",
                (*key, POLICY, resolution["resolved_at"], Jsonb(resolution)),
            )
            count += 1
            if count % 10000 == 0:
                derived = sum(p.stat().st_size for p in bundle.iterdir() if p.is_file())
                database_bytes = connection.execute(
                    "SELECT pg_database_size(current_database())"
                ).fetchone()[0]
                if derived + database_bytes > MAX_DERIVED:
                    raise ValueError("derived bundle and database exceed 20 GiB cap")
                _resource_check(bundle)
        return {
            "bundle_id": index["bundle_id"],
            "imported_history_records": count,
            "query_count": len(queries),
            "complete_source_backed_histories": True,
            "working_set_intervals": [[s.isoformat(), e.isoformat()] for s, e in merged],
            "previous_card_records": len(previous),
            "baseline_queries_included": baseline_bundle is not None,
        }

"""Restore the original Anthropic ledger from a verified dump, never a preserved store.

Procedure (owner/offline):

1. `restore_dump` checks the custom-format dump's SHA-256 against the recorded
   value and restores it into a new, empty, disposable staging database whose name
   starts with `reckoner_ledger_staging_`. The preserved Phase 1 store and its
   volume are never started, attached or written.
2. `copy_legacy_ledger` reads the staged `budget_entries` chain (budget entries,
   attempts, tasks, runs, run/threshold configurations, cohorts and referenced
   transactions), requires exactly 1,040 settled entries totalling USD .493151,
   copies them into the Phase 3 measurement database with insert-or-compare in one
   transaction, verifies the ledger hash under the accounting lock and appends
   provenance naming the dump SHA-256. Any conflict leaves the target unchanged.

No arithmetic balance or fabricated row ever substitutes for restored records.
"""

import hashlib
import subprocess
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from reckoner.contracts import content_id
from reckoner.storage.budget import ACCOUNTING_LOCK, BudgetExceeded

STAGING_PREFIX = "reckoner_ledger_staging_"
PRESERVED = {"reckoner_measured", "reckoner_smoke"}
CALLS, COST = 1040, Decimal("0.493151")
# FK order with primary keys; only rows referenced by the ledger chain are copied.
CHAIN = (
    ("threshold_configs", ("tenant_id", "config_id")),
    ("run_configs", ("tenant_id", "config_id")),
    ("cohorts", ("tenant_id", "cohort_id")),
    ("transactions", ("tenant_id", "transaction_id")),
    ("runs", ("tenant_id", "run_id")),
    ("tasks", ("tenant_id", "run_id", "task_id")),
    ("attempts", ("tenant_id", "run_id", "task_id", "call_id")),
    ("budget_entries", ("tenant_id", "run_id", "task_id", "call_id")),
)
SELECTORS = {
    "budget_entries": "SELECT * FROM reckoner.budget_entries",
    "attempts": "SELECT a.* FROM reckoner.attempts a JOIN reckoner.budget_entries b "
    "USING (tenant_id, run_id, task_id, call_id)",
    "tasks": "SELECT DISTINCT t.* FROM reckoner.tasks t JOIN reckoner.budget_entries b "
    "USING (tenant_id, run_id, task_id)",
    "runs": "SELECT DISTINCT r.* FROM reckoner.runs r JOIN reckoner.budget_entries b "
    "USING (tenant_id, run_id)",
    "run_configs": "SELECT DISTINCT c.* FROM reckoner.run_configs c JOIN reckoner.runs r "
    "USING (tenant_id, config_id) JOIN reckoner.budget_entries b ON "
    "(b.tenant_id, b.run_id)=(r.tenant_id, r.run_id)",
    "threshold_configs": "SELECT DISTINCT t.* FROM reckoner.threshold_configs t "
    "JOIN reckoner.run_configs c ON (c.tenant_id, c.threshold_config_id)="
    "(t.tenant_id, t.config_id) JOIN reckoner.runs r ON (r.tenant_id, r.config_id)="
    "(c.tenant_id, c.config_id) JOIN reckoner.budget_entries b ON "
    "(b.tenant_id, b.run_id)=(r.tenant_id, r.run_id)",
    "cohorts": "SELECT DISTINCT c.* FROM reckoner.cohorts c JOIN reckoner.runs r "
    "ON (r.tenant_id, r.bundle_id, r.purpose)=(c.tenant_id, c.bundle_id, c.purpose) "
    "JOIN reckoner.budget_entries b ON (b.tenant_id, b.run_id)=(r.tenant_id, r.run_id)",
    "transactions": "SELECT DISTINCT x.* FROM reckoner.transactions x JOIN reckoner.tasks t "
    "USING (tenant_id, transaction_id) JOIN reckoner.budget_entries b "
    "USING (tenant_id, run_id, task_id)",
}


def _database(dsn):
    return conninfo_to_dict(dsn).get("dbname", "")


def check_databases(staging: str, target: str) -> None:
    """Staging is a dedicated disposable database; neither side is a preserved store."""
    if not staging.startswith(STAGING_PREFIX):
        raise ValueError("staging must be a dedicated reckoner_ledger_staging_ database")
    if staging in PRESERVED or target in PRESERVED or target.startswith(STAGING_PREFIX):
        raise ValueError("refusing a preserved store or staging database as the target")
    if staging == target:
        raise ValueError("staging and target databases must differ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def restore_dump(dump: Path, *, expected_sha256: str, staging_dsn: str, command: list[str]):
    """Restore a verified custom-format dump into an empty disposable staging database.

    `command` is the exact pg_restore invocation (for example run inside a disposable
    container of the dump's major version); the dump is supplied on standard input.
    """
    digest = sha256_file(dump)
    if digest != expected_sha256:
        raise ValueError("dump SHA-256 differs from the recorded backup identity")
    name = _database(staging_dsn)
    if not name.startswith(STAGING_PREFIX):
        raise ValueError("staging must be a dedicated reckoner_ledger_staging_ database")
    with psycopg.connect(staging_dsn, autocommit=True) as connection:
        if connection.execute(
            "SELECT count(*) FROM pg_namespace WHERE nspname IN ('reckoner','oracle')"
        ).fetchone()[0]:
            raise ValueError("staging database must be empty before restore")
    with Path(dump).open("rb") as handle:
        subprocess.run(command, stdin=handle, check=True, capture_output=True)
    return {"dump_sha256": digest, "staging_database": name}


def _staged(staging_dsn):
    with psycopg.connect(staging_dsn, row_factory=dict_row) as connection:
        entries = connection.execute(
            "SELECT status, actual_cost FROM reckoner.budget_entries"
        ).fetchall()
        if (
            len(entries) != CALLS
            or any(e["status"] != "settled" or e["actual_cost"] is None for e in entries)
            or sum(e["actual_cost"] for e in entries) != COST
        ):
            raise ValueError("staged ledger is not 1,040 settled entries totalling USD .493151")
        return {table: connection.execute(SELECTORS[table]).fetchall() for table, _ in CHAIN}


def _copy(cursor, table, key, rows):
    for row in rows:
        columns = list(row)
        cursor.execute(
            sql.SQL("INSERT INTO reckoner.{} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
                sql.Identifier(table),
                sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.SQL(", ").join(sql.Placeholder() for _ in columns),
            ),
            [Jsonb(v) if isinstance(v, dict | list) else v for v in row.values()],
        )
        stored = cursor.execute(
            sql.SQL("SELECT {} FROM reckoner.{} WHERE {}").format(
                sql.SQL(", ").join(map(sql.Identifier, columns)),
                sql.Identifier(table),
                sql.SQL(" AND ").join(sql.SQL("{}=%s").format(sql.Identifier(k)) for k in key),
            ),
            [row[k] for k in key],
        ).fetchone()
        if stored != row:
            raise ValueError(f"restored {table} row conflicts with existing target data")
    return len(rows)


def copy_legacy_ledger(*, staging_dsn: str, target_owner_dsn: str, dump_sha256: str) -> dict:
    """Copy the verified staged ledger chain into the target and record provenance."""
    from reckoner.v1.storage.budget import ProviderBudget

    check_databases(_database(staging_dsn), _database(target_owner_dsn))
    staged = _staged(staging_dsn)
    with psycopg.connect(target_owner_dsn, autocommit=True) as connection:
        copied = {}
        try:
            with connection.transaction():
                cursor = connection.cursor(row_factory=dict_row)
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
                for table, key in CHAIN:
                    copied[table] = _copy(cursor, table, key, staged[table])
                budget = ProviderBudget(connection)
                rows, digest = budget._legacy(cursor)
                if len(rows) != CALLS or sum(r["actual_cost"] for r in rows) != COST:
                    raise ValueError("target ledger differs from the restored ledger")
                for tenant in sorted({r["tenant_id"] for r in rows}):
                    document = {
                        "tenant_id": tenant,
                        "provider": "anthropic",
                        "source": "phase1-completion-custom-dump",
                        "dump_sha256": dump_sha256,
                        "call_count": CALLS,
                        "settled_cost": ".493151",
                        "ledger_sha256": digest,
                    }
                    cursor.execute(
                        "INSERT INTO reckoner.v1_legacy_provenance (tenant_id,provenance_id,"
                        "document) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                        (tenant, content_id(document), Jsonb(document)),
                    )
        except BudgetExceeded as error:
            raise ValueError(str(error)) from error
        verified = ProviderBudget(connection).verify_legacy()
    return {
        "dump_sha256": dump_sha256,
        "copied": copied,
        "call_count": CALLS,
        "settled_cost": format(COST, "f"),
        "ledger_sha256": verified,
        "dataset_simulated": True,
    }


EVIDENCE_SCHEMA = "reckoner-settlement-evidence-v1"
EVIDENCE_KINDS = {"provider-usage-record", "provider-invoice-line", "provider-request-log"}


def load_settlement_evidence(path: Path) -> dict:
    """A stored provider usage document, identified by the SHA-256 of its bytes."""
    import json

    payload = Path(path).read_bytes()
    document = json.loads(payload)
    return {"document": document, "sha256": hashlib.sha256(payload).hexdigest()}


def _persisted_usage(body):
    """Usage counts a persisted provider response itself reported, if any."""
    if not isinstance(body, dict):
        return None
    source = body.get("usage") if isinstance(body.get("usage"), dict) else body
    counts = {key: source.get(key) for key in ("input_tokens", "output_tokens")}
    if all(type(v) is int and v >= 0 for v in counts.values()):
        return counts
    return None


def reconcile_call(connection, call_id: str, usage: dict, *, evidence: dict | None) -> dict:
    """Owner reconciliation of one uncertain or never-answered call from provider evidence.

    `evidence` is `load_settlement_evidence(path)`: a stored provider record
    (`reckoner-settlement-evidence-v1`: call_id, source_kind, reference, usage) and its
    SHA-256. Entered counts must equal the evidence and any usage the persisted
    response itself reported; zero is accepted only when the evidence states zero.
    Without evidence nothing is settled: a never-answered call stays uncertain and is
    never auto-zeroed. Cost is computed from the call's recorded protocol price.
    """
    from reckoner.v1 import pricing
    from reckoner.v1.storage.budget import ProviderBudget

    if evidence is None:
        raise ValueError("provider evidence is required; the call stays uncertain")
    if set(usage) != {"input_tokens", "output_tokens"} or any(
        type(v) is not int or v < 0 for v in usage.values()
    ):
        raise ValueError("reconciliation needs exact nonnegative token counts")
    record, digest = evidence["document"], evidence["sha256"]
    if (
        not isinstance(record, dict)
        or set(record) != {"schema_version", "call_id", "source_kind", "reference", "usage"}
        or record["schema_version"] != EVIDENCE_SCHEMA
        or record["source_kind"] not in EVIDENCE_KINDS
        or not isinstance(record["reference"], str)
        or not record["reference"].strip()
    ):
        raise ValueError("evidence must be a stored provider usage record")
    if record["call_id"] != call_id:
        raise ValueError("evidence belongs to another call")
    if record["usage"] != usage:
        raise ValueError(
            "entered usage differs from the provider evidence (zero needs stated zero)"
        )
    cursor = connection.cursor(row_factory=dict_row)
    row = cursor.execute(
        "SELECT c.provider, c.tenant_id, x.document->'prices' AS prices, r.call_id AS answered, "
        "r.body, EXISTS (SELECT 1 FROM reckoner.v1_settlements s WHERE s.call_id=c.call_id "
        "AND s.status='settled') AS settled, EXISTS (SELECT 1 FROM reckoner.v1_settlements u "
        "WHERE u.call_id=c.call_id AND u.status='uncertain') AS uncertain, EXISTS (SELECT 1 "
        "FROM reckoner.v1_protocol_closures z WHERE z.protocol_id=c.protocol_id) AS closed, "
        "EXISTS (SELECT 1 FROM reckoner.v1_provider_state p WHERE p.provider=c.provider "
        "AND p.active_call=c.call_id) AS in_flight FROM reckoner.v1_provider_calls c "
        "JOIN reckoner.v1_protocol_authorizations a USING (protocol_id) "
        "JOIN reckoner.v1_experiment_protocols x USING (protocol_sha256) "
        "LEFT JOIN reckoner.v1_provider_responses r ON r.call_id=c.call_id WHERE c.call_id=%s",
        (call_id,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown call under a recorded protocol")
    if row["settled"]:
        raise ValueError("call is already settled; settlements are immutable")
    if not row["uncertain"]:
        raise ValueError("only calls recorded as uncertain can be reconciled")
    if row["in_flight"]:
        raise ValueError("call is still in flight as the provider's active dispatch")
    if not row["closed"]:
        raise ValueError("close the call's protocol envelope before reconciling it")
    persisted_request = (
        (row["body"] or {}).get("provider_request_id") if isinstance(row["body"], dict) else None
    )
    if persisted_request is not None and persisted_request != record["reference"]:
        raise ValueError("evidence reference differs from the persisted provider request id")
    if row["answered"] is None:
        state = "never-answered"  # reserved, no response persisted
    elif row["body"] is None:
        state = "dispatched-unknown"  # dispatched, outcome and billing unknown
    else:
        state = "responded-unknown-billing"
    reported = _persisted_usage(row["body"])
    if reported is not None and reported != usage:
        raise ValueError("entered usage differs from the persisted response usage")
    if not pricing.matches(row["provider"], row["prices"] or {}):
        raise ValueError("recorded protocol prices are not the pinned prices")
    cost = pricing.cost(row["provider"], usage)
    document = {
        "call_id": call_id,
        "prior_state": state,
        "source_kind": record["source_kind"],
        "reference": record["reference"],
        "document_sha256": digest,
        "usage": usage,
        "cost": format(cost.normalize(), "f"),
    }
    with connection.transaction():
        ProviderBudget(connection).settle(call_id, usage, cost)
        cursor.execute(
            "INSERT INTO reckoner.v1_settlement_evidence (tenant_id, call_id, document) "
            "VALUES (%s,%s,%s)",
            (row["tenant_id"], call_id, Jsonb(document)),
        )
    return {
        "call_id": call_id,
        "status": "settled",
        "prior_state": state,
        "cost": format(cost.normalize(), "f"),
        "usage": usage,
        "evidence_sha256": digest,
    }

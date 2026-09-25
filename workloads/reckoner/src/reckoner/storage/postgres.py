"""Concrete PostgreSQL repository for baseline state."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from jsonschema import ValidationError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from reckoner.contracts import content_id, validate_document, validate_threshold_config
from reckoner.data.artifacts import verify_bundle
from reckoner.storage.budget import ACCOUNTING_LOCK, RunBusy

ROOT = Path(__file__).resolve().parents[5]
SCHEMAS = ROOT / "contracts" / "schemas"
RUNNER_LOCK = 732019101
REQUIRED_MIGRATION = "004_evaluation_reporting.sql"


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    return value


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _read_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("artifact JSON must be an object")
    return document


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    documents = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            document = json.loads(line)
            if not isinstance(document, dict):
                raise ValueError("artifact JSONL must contain objects")
            documents.append(document)
    return documents


def _insert_document(
    cursor: psycopg.Cursor,
    table: str,
    columns: tuple[str, ...],
    values: tuple[Any, ...],
    key_columns: tuple[str, ...],
    document: dict[str, Any],
) -> None:
    column_sql = ", ".join((*columns, "document"))
    placeholders = ", ".join(["%s"] * (len(values) + 1))
    conflict = ", ".join(key_columns)
    cursor.execute(
        f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) DO NOTHING",
        (*values, Jsonb(document)),
    )
    where = " AND ".join(f"{column} = %s" for column in key_columns)
    key_indexes = [columns.index(column) for column in key_columns]
    existing = cursor.execute(
        f"SELECT document FROM {table} WHERE {where}",
        tuple(values[index] for index in key_indexes),
    ).fetchone()
    if existing is None or existing["document"] != document:
        raise ValueError(f"immutable {table.rsplit('.', 1)[-1]} identity mismatch")


class PostgresRepository:
    """Owns one explicit PostgreSQL connection and its transaction boundaries."""

    def __init__(self, dsn: str):
        self._connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)

    def __enter__(self) -> PostgresRepository:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def import_bundle(self, bundle: Path) -> None:
        """Import verified runtime and oracle artifacts as an owner-only transaction."""
        bundle = Path(bundle)
        index = verify_bundle(bundle)
        with self._connection.transaction():
            cursor = self._connection.cursor()
            for purpose, cohort in index["cohorts"].items():
                runtime_meta = index["files"][cohort["runtime_file"]]
                oracle_meta = index["files"][cohort["oracle_file"]]
                runtime = _read_jsonl(bundle / runtime_meta["path"])
                oracle = _read_jsonl(bundle / oracle_meta["path"])
                for transaction in runtime:
                    _insert_document(
                        cursor,
                        "reckoner.transactions",
                        ("tenant_id", "transaction_id"),
                        (transaction["tenant_id"], transaction["transaction_id"]),
                        ("tenant_id", "transaction_id"),
                        transaction,
                    )
                for label in oracle:
                    _insert_document(
                        cursor,
                        "oracle.oracle_labels",
                        ("tenant_id", "transaction_id", "oracle_version", "label"),
                        (
                            label["tenant_id"],
                            label["transaction_id"],
                            label["oracle_version"],
                            label["label"],
                        ),
                        ("tenant_id", "transaction_id", "oracle_version"),
                        label,
                    )
                for logical_name in cohort["manifest_files"]:
                    manifest = _read_json(bundle / index["files"][logical_name]["path"])
                    _insert_document(
                        cursor,
                        "reckoner.cohorts",
                        ("tenant_id", "cohort_id", "purpose", "bundle_id"),
                        (
                            manifest["tenant_id"],
                            manifest["cohort_id"],
                            purpose,
                            index["bundle_id"],
                        ),
                        ("tenant_id", "cohort_id"),
                        manifest,
                    )
                    for transaction_id in manifest["selected_transaction_ids"]:
                        cursor.execute(
                            """
                            INSERT INTO reckoner.cohort_members
                              (tenant_id, cohort_id, transaction_id)
                            VALUES (%s, %s, %s)
                            ON CONFLICT DO NOTHING
                            """,
                            (manifest["tenant_id"], manifest["cohort_id"], transaction_id),
                        )

    def register_threshold_config(self, document: dict[str, Any]) -> None:
        """Persist one tenant-owned threshold snapshot with insert-or-compare semantics."""
        plain = _plain(document)
        try:
            validate_threshold_config(plain, SCHEMAS / "threshold-config-v1.schema.json")
        except (ValidationError, ValueError) as error:
            raise ValueError("invalid threshold configuration") from error
        with self._connection.transaction():
            _insert_document(
                self._connection.cursor(),
                "reckoner.threshold_configs",
                ("tenant_id", "config_id"),
                (plain["tenant_id"], plain["config_id"]),
                ("tenant_id", "config_id"),
                plain,
            )

    @staticmethod
    def _validated_price(document: dict[str, Any]) -> dict[str, Any]:
        plain = _plain(document)
        try:
            validate_document(plain, SCHEMAS / "price-table-v1.schema.json")
        except ValidationError as error:
            raise ValueError("invalid price document") from error
        body = {key: value for key, value in plain.items() if key != "price_table_version"}
        if plain["price_table_version"] != content_id(body):
            raise ValueError("price identity does not match contents")
        return plain

    def create_run(
        self,
        run_id: str,
        purpose: str,
        config: dict,
        bundle_id: str,
        *,
        price: dict | None = None,
        execution_mode: str = "paid",
    ) -> None:
        """Freeze a shared config per tenant and create every cohort task atomically."""
        if not run_id:
            raise ValueError("run ID is required")
        if purpose not in {"pilot", "baseline"}:
            raise ValueError("invalid run purpose")
        if execution_mode not in {"paid", "test"}:
            raise ValueError("invalid execution mode")
        plain = _plain(config)
        try:
            validate_document(plain, SCHEMAS / "run-config-v1.schema.json")
        except ValidationError as error:
            raise ValueError("invalid run configuration") from error
        body = {key: value for key, value in plain.items() if key != "config_id"}
        if plain["config_id"] != content_id(body):
            raise ValueError("run config identity does not match contents")
        price_document = self._validated_price(price) if price is not None else None
        if price_document is not None and (
            price_document["price_table_version"] != plain["price_table_version"]
            or price_document["model"] != plain["model"]
        ):
            raise ValueError("price document does not match run configuration")

        with self._connection.transaction():
            cursor = self._connection.cursor()
            cohorts = cursor.execute(
                """
                SELECT tenant_id, cohort_id
                FROM reckoner.cohorts
                WHERE bundle_id = %s AND purpose = %s
                ORDER BY tenant_id
                """,
                (bundle_id, purpose),
            ).fetchall()
            if not cohorts:
                raise ValueError("bundle cohort is not imported")
            tenants = {row["tenant_id"] for row in cohorts}
            threshold_ids = plain["threshold_config_ids"]
            if set(threshold_ids) != tenants:
                raise ValueError("threshold tenant coverage does not match bundle")

            for tenant_id in sorted(tenants):
                threshold_id = threshold_ids[tenant_id]
                threshold = cursor.execute(
                    """
                    SELECT document FROM reckoner.threshold_configs
                    WHERE tenant_id = %s AND config_id = %s
                    """,
                    (tenant_id, threshold_id),
                ).fetchone()
                if threshold is None or threshold["document"]["tenant_id"] != tenant_id:
                    raise ValueError("threshold configuration ownership mismatch")

                _insert_document(
                    cursor,
                    "reckoner.run_configs",
                    ("tenant_id", "config_id", "threshold_config_id"),
                    (tenant_id, plain["config_id"], threshold_id),
                    ("tenant_id", "config_id"),
                    plain,
                )
                if price_document is not None:
                    _insert_document(
                        cursor,
                        "reckoner.price_tables",
                        ("tenant_id", "price_table_version"),
                        (tenant_id, price_document["price_table_version"]),
                        ("tenant_id", "price_table_version"),
                        price_document,
                    )
                stored_price = cursor.execute(
                    """
                    SELECT document FROM reckoner.price_tables
                    WHERE tenant_id = %s AND price_table_version = %s
                    """,
                    (tenant_id, plain["price_table_version"]),
                ).fetchone()
                if stored_price is None:
                    raise ValueError("price document is not registered")
                if self._validated_price(stored_price["document"]) != stored_price["document"]:
                    raise ValueError("invalid stored price document")
                cursor.execute(
                    """
                    INSERT INTO reckoner.runs
                      (tenant_id, run_id, purpose, config_id, bundle_id, execution_mode)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, run_id) DO NOTHING
                    """,
                    (
                        tenant_id,
                        run_id,
                        purpose,
                        plain["config_id"],
                        bundle_id,
                        execution_mode,
                    ),
                )
                existing = cursor.execute(
                    """
                    SELECT purpose, config_id, bundle_id, execution_mode FROM reckoner.runs
                    WHERE tenant_id = %s AND run_id = %s
                    """,
                    (tenant_id, run_id),
                ).fetchone()
                if existing != {
                    "purpose": purpose,
                    "config_id": plain["config_id"],
                    "bundle_id": bundle_id,
                    "execution_mode": execution_mode,
                }:
                    raise ValueError("immutable run identity mismatch")

            for cohort in cohorts:
                transaction_ids = cursor.execute(
                    """
                    SELECT transaction_id FROM reckoner.cohort_members
                    WHERE tenant_id = %s AND cohort_id = %s
                    ORDER BY transaction_id
                    """,
                    (cohort["tenant_id"], cohort["cohort_id"]),
                ).fetchall()
                for row in transaction_ids:
                    task_id = content_id(
                        {
                            "tenant_id": cohort["tenant_id"],
                            "run_id": run_id,
                            "transaction_id": row["transaction_id"],
                        }
                    )
                    cursor.execute(
                        """
                        INSERT INTO reckoner.tasks
                          (tenant_id, run_id, task_id, transaction_id, status)
                        VALUES (%s, %s, %s, %s, 'pending')
                        ON CONFLICT (tenant_id, run_id, task_id) DO NOTHING
                        """,
                        (cohort["tenant_id"], run_id, task_id, row["transaction_id"]),
                    )
                    existing = cursor.execute(
                        """
                        SELECT transaction_id FROM reckoner.tasks
                        WHERE tenant_id = %s AND run_id = %s AND task_id = %s
                        """,
                        (cohort["tenant_id"], run_id, task_id),
                    ).fetchone()
                    if existing != {"transaction_id": row["transaction_id"]}:
                        raise ValueError("immutable task identity mismatch")

    def run_context(self, run_id: str) -> dict[str, Any]:
        """Load one immutable cross-tenant run context without oracle access."""
        rows = self._connection.execute(
            """
            SELECT r.tenant_id, r.run_id, r.purpose, r.config_id, r.bundle_id,
                   r.execution_mode, r.provider_call_mode, r.preflight_code_revision,
                   c.document AS config, p.document AS price,
                   tc.document AS thresholds
            FROM reckoner.runs r
            JOIN reckoner.run_configs c
              ON c.tenant_id = r.tenant_id AND c.config_id = r.config_id
            JOIN reckoner.threshold_configs tc
              ON tc.tenant_id = r.tenant_id
             AND tc.config_id = c.threshold_config_id
            LEFT JOIN reckoner.price_tables p
              ON p.tenant_id = r.tenant_id
             AND p.price_table_version = c.document->>'price_table_version'
            WHERE r.run_id = %s
            ORDER BY r.tenant_id
            """,
            (run_id,),
        ).fetchall()
        if not rows:
            raise ValueError("unknown run identity")
        first = rows[0]
        stable = (
            "run_id",
            "purpose",
            "config_id",
            "bundle_id",
            "execution_mode",
            "provider_call_mode",
            "preflight_code_revision",
            "config",
        )
        if any(any(row[key] != first[key] for key in stable) for row in rows[1:]):
            raise ValueError("cross-tenant run identity mismatch")
        if any(row["price"] is None for row in rows):
            raise ValueError("price document is unavailable")
        if any(row["price"] != rows[0]["price"] for row in rows[1:]):
            raise ValueError("cross-tenant price identity mismatch")
        config = _plain(first["config"])
        try:
            validate_document(config, SCHEMAS / "run-config-v1.schema.json")
        except ValidationError as error:
            raise ValueError("invalid stored run configuration") from error
        config_body = {key: value for key, value in config.items() if key != "config_id"}
        if config["config_id"] != content_id(config_body):
            raise ValueError("stored run configuration identity mismatch")
        price = self._validated_price(first["price"])
        if (
            price["price_table_version"] != config["price_table_version"]
            or price["model"] != config["model"]
        ):
            raise ValueError("stored price does not match run configuration")
        thresholds = {}
        for row in rows:
            document = _plain(row["thresholds"])
            try:
                validate_threshold_config(document, SCHEMAS / "threshold-config-v1.schema.json")
            except (ValidationError, ValueError) as error:
                raise ValueError("invalid stored threshold configuration") from error
            if config["threshold_config_ids"].get(row["tenant_id"]) != document["config_id"]:
                raise ValueError("stored threshold configuration identity mismatch")
            thresholds[row["tenant_id"]] = document
        return {
            key: _plain(first[key])
            for key in (
                "run_id",
                "purpose",
                "config_id",
                "bundle_id",
                "execution_mode",
                "provider_call_mode",
                "preflight_code_revision",
                "config",
                "price",
            )
        } | {"thresholds": thresholds}

    def pilot_gate_satisfied(self, run_id: str) -> bool:
        """Return whether a paid baseline has one matching paid 20-case pilot."""
        context = self.run_context(run_id)
        if context["purpose"] != "baseline":
            return True
        row = self._connection.execute(
            """
            SELECT EXISTS (
              SELECT 1
              FROM reckoner.runs candidate
              WHERE candidate.purpose = 'pilot'
                AND candidate.execution_mode = 'paid'
                AND candidate.provider_call_mode = 'measured'
                AND candidate.config_id = %s
                AND candidate.bundle_id = %s
                AND (SELECT count(*) FROM reckoner.tasks t
                     WHERE t.run_id = candidate.run_id) = 20
                AND (SELECT count(*) FROM reckoner.tasks t
                     WHERE t.run_id = candidate.run_id AND t.status = 'completed') = 20
                AND (SELECT count(*) FROM reckoner.decisions d
                     WHERE d.run_id = candidate.run_id) = 20
                AND (SELECT count(*) FROM reckoner.budget_entries b
                     WHERE b.run_id = candidate.run_id
                       AND b.status = 'settled' AND b.usage IS NOT NULL) = 20
                AND (SELECT count(*) FROM reckoner.runner_telemetry_outbox o
                     WHERE o.run_id = candidate.run_id AND o.status = 'exported') = 20
                AND NOT EXISTS (
                  SELECT 1 FROM reckoner.tasks t
                  WHERE t.run_id = candidate.run_id
                    AND (t.request_document IS NULL OR length(t.request_sha256) <> 64
                      OR t.input_token_estimate IS NULL
                      OR t.reservation_input_tokens IS NULL
                      OR t.reservation_cost IS NULL OR length(t.trace_id) <> 32
                      OR length(t.span_id) <> 16 OR length(t.provider_span_id) <> 16
                      OR t.event_id IS NULL)
                )
                AND NOT EXISTS (
                  SELECT 1
                  FROM reckoner.attempts a
                  JOIN reckoner.tasks t
                    ON t.tenant_id = a.tenant_id AND t.run_id = a.run_id
                   AND t.task_id = a.task_id
                  WHERE a.run_id = candidate.run_id
                    AND a.actual_cost IS NOT NULL AND a.usage IS NOT NULL
                    AND (
                      (a.usage->>'input_tokens')::numeric > t.reservation_input_tokens
                      OR (a.usage->>'output_tokens')::numeric
                           > (t.request_document->>'max_tokens')::numeric
                    )
                )
            ) AS satisfied
            """,
            (context["config_id"], context["bundle_id"]),
        ).fetchone()
        return bool(row["satisfied"])

    def persist_preflight(
        self,
        run_id: str,
        code_revision: str,
        provider_call_mode: str,
        plans: list[dict[str, Any]],
    ) -> None:
        """Insert or compare all request estimates as one immutable preflight."""
        if not code_revision:
            raise ValueError("code revision is required")
        if provider_call_mode not in {"fake", "measured"}:
            raise ValueError("invalid provider call mode")
        with self._connection.transaction():
            cursor = self._connection.cursor()
            runs = cursor.execute(
                "SELECT preflight_code_revision, provider_call_mode FROM reckoner.runs "
                "WHERE run_id = %s FOR UPDATE",
                (run_id,),
            ).fetchall()
            if not runs:
                raise ValueError("unknown run identity")
            revisions = {row["preflight_code_revision"] for row in runs}
            if revisions - {None, code_revision}:
                raise ValueError("preflight code revision changed")
            modes = {row["provider_call_mode"] for row in runs}
            if modes - {None, provider_call_mode}:
                raise ValueError("preflight provider call mode changed")
            cursor.execute(
                """
                UPDATE reckoner.runs
                SET preflight_code_revision = %s, provider_call_mode = %s,
                    preflight_at = COALESCE(preflight_at, now())
                WHERE run_id = %s
                """,
                (code_revision, provider_call_mode, run_id),
            )
            for plan in plans:
                row = cursor.execute(
                    """
                    SELECT status, request_document, request_sha256, input_token_estimate,
                           reservation_input_tokens, reservation_cost, trace_id, span_id,
                           provider_span_id, event_id
                    FROM reckoner.tasks
                    WHERE tenant_id = %s AND run_id = %s AND task_id = %s
                    FOR UPDATE
                    """,
                    (plan["tenant_id"], run_id, plan["task_id"]),
                ).fetchone()
                if row is None or row["status"] != "pending":
                    raise ValueError("preflight task is not pending")
                expected = {
                    "request_document": plan["request"],
                    "request_sha256": plan["request_sha256"],
                    "input_token_estimate": plan["input_token_estimate"],
                    "reservation_input_tokens": plan["reservation_input_tokens"],
                    "reservation_cost": plan["reservation_cost"],
                    "trace_id": plan["trace_id"],
                    "span_id": plan["span_id"],
                    "provider_span_id": plan["provider_span_id"],
                    "event_id": plan["event_id"],
                }
                if row["request_document"] is None:
                    cursor.execute(
                        """
                        UPDATE reckoner.tasks
                        SET request_document = %s, request_sha256 = %s,
                            input_token_estimate = %s, reservation_input_tokens = %s,
                            reservation_cost = %s, trace_id = %s, span_id = %s,
                            provider_span_id = %s, event_id = %s
                        WHERE tenant_id = %s AND run_id = %s AND task_id = %s
                        """,
                        (
                            Jsonb(plan["request"]),
                            plan["request_sha256"],
                            plan["input_token_estimate"],
                            plan["reservation_input_tokens"],
                            plan["reservation_cost"],
                            plan["trace_id"],
                            plan["span_id"],
                            plan["provider_span_id"],
                            plan["event_id"],
                            plan["tenant_id"],
                            run_id,
                            plan["task_id"],
                        ),
                    )
                elif any(row[key] != value for key, value in expected.items()):
                    raise ValueError("immutable preflight request identity mismatch")

    def reconcile_dispatched(self) -> int:
        """Turn every crash-left dispatched attempt into durable uncertain evidence."""
        with self._connection.transaction():
            cursor = self._connection.cursor()
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
            rows = cursor.execute(
                """
                SELECT b.call_id FROM reckoner.budget_entries b
                JOIN reckoner.attempts a ON a.call_id = b.call_id
                WHERE a.status = 'dispatched'
                ORDER BY b.call_id
                FOR UPDATE OF b
                """,
            ).fetchall()
            for row in rows:
                self._mark_attempt_uncertain(cursor, row["call_id"], "process_interrupted")
        return len(rows)

    def has_uncertain_attempts(self) -> bool:
        row = self._connection.execute(
            "SELECT EXISTS (SELECT 1 FROM reckoner.attempts WHERE status = 'uncertain') "
            "AS uncertain"
        ).fetchone()
        return bool(row["uncertain"])

    @staticmethod
    def _mark_attempt_uncertain(cursor: psycopg.Cursor, call_id: str, category: str) -> None:
        budget = cursor.execute(
            "SELECT tenant_id, run_id, task_id FROM reckoner.budget_entries "
            "WHERE call_id = %s FOR UPDATE",
            (call_id,),
        ).fetchone()
        if budget is None:
            raise ValueError("unknown call identity")
        entry = cursor.execute(
            "SELECT tenant_id, run_id, task_id FROM reckoner.attempts "
            "WHERE call_id = %s FOR UPDATE",
            (call_id,),
        ).fetchone()
        if entry is None:
            raise ValueError("unknown call identity")
        cursor.execute(
            """
            UPDATE reckoner.attempts
            SET status = 'uncertain', error_category = %s, settled_at = now()
            WHERE call_id = %s AND status = 'dispatched'
            """,
            (category, call_id),
        )
        cursor.execute(
            """
            UPDATE reckoner.budget_entries
            SET status = 'uncertain', settled_at = now()
            WHERE call_id = %s AND status = 'reserved'
            """,
            (call_id,),
        )
        cursor.execute(
            """
            UPDATE reckoner.tasks SET status = 'uncertain'
            WHERE tenant_id = %s AND run_id = %s AND task_id = %s
              AND status = 'dispatched'
            """,
            (entry["tenant_id"], entry["run_id"], entry["task_id"]),
        )
        cursor.execute(
            "UPDATE reckoner.runs SET status = 'incomplete' WHERE run_id = %s",
            (entry["run_id"],),
        )

    def mark_attempt_uncertain(self, call_id: str, category: str) -> None:
        with self._connection.transaction():
            cursor = self._connection.cursor()
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
            self._mark_attempt_uncertain(cursor, call_id, category)

    def set_run_status(self, run_id: str, status: str) -> None:
        if status not in {"pending", "running", "complete", "incomplete", "blocked"}:
            raise ValueError("invalid run status")
        with self._connection.transaction():
            updated = self._connection.execute(
                "UPDATE reckoner.runs SET status = %s WHERE run_id = %s",
                (status, run_id),
            ).rowcount
            if not updated:
                raise ValueError("unknown run identity")

    @staticmethod
    def _refresh_run_status(cursor: psycopg.Cursor, run_id: str) -> None:
        counts = cursor.execute(
            """
            SELECT status, count(*) AS count FROM reckoner.tasks
            WHERE run_id = %s GROUP BY status
            """,
            (run_id,),
        ).fetchall()
        by_status = {row["status"]: row["count"] for row in counts}
        if by_status.get("uncertain"):
            status = "incomplete"
        elif by_status.get("pending") or by_status.get("dispatched"):
            status = "running"
        elif by_status.get("failed"):
            status = "incomplete"
        else:
            status = "complete"
        cursor.execute(
            "UPDATE reckoner.runs SET status = %s WHERE run_id = %s",
            (status, run_id),
        )

    def refresh_run_status(self, run_id: str) -> None:
        with self._connection.transaction():
            self._refresh_run_status(self._connection.cursor(), run_id)

    def finish_attempt(
        self,
        reservation: dict,
        result: dict | None,
        error_category: str | None,
        timing: dict,
    ) -> None:
        """Persist result, usage, task state, budget settlement, and OTLP atomically."""
        from reckoner.baseline.pricing import observed_cost
        from reckoner.baseline.provider import InvalidResponse, parse_decision
        from reckoner.telemetry.events import store_provider_span

        if (result is None) == (error_category is None):
            raise ValueError("exactly one result or error category is required")
        context = self.run_context(reservation["run_id"])
        prices = context["price"]
        usage = None
        actual = None
        outcome = None
        final_status = "uncertain"
        safe_error = error_category
        if result is not None:
            usage = {
                key: result.get(key)
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_creation_tokens",
                )
            }
            actual = observed_cost(result, prices)
            try:
                outcome = parse_decision(result)
            except InvalidResponse:
                safe_error = "invalid_response"
            if actual is not None:
                final_status = "completed" if outcome is not None else "failed"

        with self._connection.transaction():
            cursor = self._connection.cursor()
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
            budget = cursor.execute(
                "SELECT call_id FROM reckoner.budget_entries WHERE call_id = %s FOR UPDATE",
                (reservation["call_id"],),
            ).fetchone()
            if budget is None:
                raise ValueError("unknown call identity")
            attempt = cursor.execute(
                """
                SELECT a.*, t.request_document, t.reservation_input_tokens,
                       t.event_id, t.span_id AS task_span_id,
                       r.config_id, r.execution_mode,
                       r.bundle_id, r.preflight_code_revision, r.provider_call_mode,
                       c.threshold_config_id, c.document AS config,
                       ch.cohort_id
                FROM reckoner.attempts a
                JOIN reckoner.tasks t
                  ON t.tenant_id = a.tenant_id AND t.run_id = a.run_id
                 AND t.task_id = a.task_id
                JOIN reckoner.runs r
                  ON r.tenant_id = a.tenant_id AND r.run_id = a.run_id
                JOIN reckoner.run_configs c
                  ON c.tenant_id = r.tenant_id AND c.config_id = r.config_id
                JOIN reckoner.cohort_members cm
                  ON cm.tenant_id = t.tenant_id AND cm.transaction_id = t.transaction_id
                JOIN reckoner.cohorts ch
                  ON ch.tenant_id = cm.tenant_id AND ch.cohort_id = cm.cohort_id
                 AND ch.purpose = r.purpose AND ch.bundle_id = r.bundle_id
                WHERE a.call_id = %s
                FOR UPDATE OF a, t
                """,
                (reservation["call_id"],),
            ).fetchone()
            if attempt is None or attempt["status"] != "dispatched":
                raise ValueError("attempt is not dispatched")

            if actual is not None and (
                usage["input_tokens"] > attempt["reservation_input_tokens"]
                or usage["output_tokens"] > attempt["request_document"]["max_tokens"]
            ):
                outcome = None
                final_status = "failed"
                safe_error = "provider_usage_bound_exceeded"

            store_provider_span(
                cursor,
                attempt=attempt,
                result=result,
                status=final_status,
                error_category=safe_error,
                timing=timing,
                actual_cost=actual,
                usage=usage,
                price_table_version=context["config"]["price_table_version"],
            )

            cursor.execute(
                """
                UPDATE reckoner.attempts
                SET status = %s, requested_model = %s, reported_model = %s,
                    response_document = %s, usage = %s, actual_cost = %s,
                    error_category = %s, started_at = %s, ended_at = %s,
                    duration_ms = %s, settled_at = now()
                WHERE call_id = %s
                """,
                (
                    "responded" if final_status == "completed" else final_status,
                    attempt["request_document"]["model"],
                    result.get("reported_model") if result else None,
                    Jsonb(result) if result else None,
                    Jsonb(usage) if usage is not None else None,
                    actual,
                    safe_error,
                    timing["started_at"],
                    timing["ended_at"],
                    Decimal(str(timing["duration_ms"])),
                    reservation["call_id"],
                ),
            )
            if actual is None:
                cursor.execute(
                    """
                    UPDATE reckoner.budget_entries
                    SET status = 'uncertain', usage = %s, settled_at = now()
                    WHERE call_id = %s
                    """,
                    (Jsonb(usage) if usage is not None else None, reservation["call_id"]),
                )
            else:
                cursor.execute(
                    """
                    UPDATE reckoner.budget_entries
                    SET status = 'settled', actual_cost = %s, usage = %s, settled_at = now()
                    WHERE call_id = %s
                    """,
                    (actual, Jsonb(usage), reservation["call_id"]),
                )
            cursor.execute(
                """
                UPDATE reckoner.tasks SET status = %s
                WHERE tenant_id = %s AND run_id = %s AND task_id = %s
                """,
                (
                    final_status,
                    attempt["tenant_id"],
                    attempt["run_id"],
                    attempt["task_id"],
                ),
            )
            if outcome is not None:
                decision_id = content_id({"call_id": reservation["call_id"], "outcome": outcome})
                cursor.execute(
                    """
                    INSERT INTO reckoner.decisions
                      (tenant_id, run_id, task_id, decision_id, call_id, outcome,
                       cohort_id, config_id, prompt_version, threshold_config_id,
                       requested_model, reported_model, document)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        attempt["tenant_id"],
                        attempt["run_id"],
                        attempt["task_id"],
                        decision_id,
                        reservation["call_id"],
                        outcome,
                        attempt["cohort_id"],
                        attempt["config_id"],
                        attempt["config"]["prompt_version"],
                        attempt["threshold_config_id"],
                        attempt["request_document"]["model"],
                        result["reported_model"],
                        Jsonb({"outcome": outcome}),
                    ),
                )
            self._refresh_run_status(cursor, attempt["run_id"])

    def outbox_rows(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT tenant_id, event_id, run_id, task_id, status, payload, created_at
            FROM reckoner.runner_telemetry_outbox
            WHERE run_id = %s ORDER BY tenant_id, event_id
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_outbox_status(self, run_id: str, event_ids: list[str], status: str) -> None:
        """Durably record the local export outcome for runner-produced evidence."""
        if status not in {"exported", "failed"}:
            raise ValueError("invalid outbox status")
        if not event_ids:
            return
        with self._connection.transaction():
            rows = self._connection.execute(
                """
                UPDATE reckoner.runner_telemetry_outbox SET status = %s
                WHERE run_id = %s AND event_id = ANY(%s)
                RETURNING event_id
                """,
                (status, run_id, event_ids),
            ).fetchall()
            if {row["event_id"] for row in rows} != set(event_ids):
                raise ValueError("outbox identity mismatch")

    def evaluation_outbox_rows(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """
            SELECT tenant_id, event_id, run_id, task_id, status, payload, created_at
            FROM reckoner.evaluator_telemetry_outbox
            WHERE run_id = %s ORDER BY tenant_id, event_id
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def set_evaluation_outbox_status(self, run_id: str, event_ids: list[str], status: str) -> None:
        if status not in {"exported", "failed"}:
            raise ValueError("invalid outbox status")
        if not event_ids:
            return
        with self._connection.transaction():
            rows = self._connection.execute(
                """
                UPDATE reckoner.evaluator_telemetry_outbox SET status = %s
                WHERE run_id = %s AND event_id = ANY(%s)
                RETURNING event_id
                """,
                (status, run_id, event_ids),
            ).fetchall()
            if {row["event_id"] for row in rows} != set(event_ids):
                raise ValueError("evaluation outbox identity mismatch")

    def pending_tasks(self, run_id: str) -> list[dict]:
        rows = self._connection.execute(
            """
            SELECT t.tenant_id, t.run_id, t.task_id, x.document AS transaction, t.status,
                   t.request_document AS request, t.request_sha256,
                   t.input_token_estimate, t.reservation_input_tokens,
                   t.reservation_cost, t.trace_id, t.span_id, t.provider_span_id, t.event_id
            FROM reckoner.tasks t
            JOIN reckoner.transactions x
              ON x.tenant_id = t.tenant_id AND x.transaction_id = t.transaction_id
            WHERE t.run_id = %s AND t.status = 'pending'
            ORDER BY t.tenant_id, t.task_id
            """,
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def snapshot(self, run_id: str, tenant_id: str | None) -> dict:
        condition = "run_id = %s"
        parameters: tuple[Any, ...] = (run_id,)
        if tenant_id is not None:
            condition += " AND tenant_id = %s"
            parameters += (tenant_id,)
        result = {}
        for key, table, ordering in (
            ("runs", "runs", "tenant_id"),
            ("tasks", "tasks", "(status = 'pending'), tenant_id, task_id"),
            ("attempts", "attempts", "tenant_id, task_id, call_id"),
            ("decisions", "decisions", "tenant_id, task_id"),
            ("budget_entries", "budget_entries", "tenant_id, task_id, call_id"),
        ):
            rows = self._connection.execute(
                f"SELECT * FROM reckoner.{table} WHERE {condition} ORDER BY {ordering}",
                parameters,
            ).fetchall()
            result[key] = [dict(row) for row in rows]
        counts = self._connection.execute(
            f"SELECT status, count(*) AS count FROM reckoner.tasks "
            f"WHERE {condition} GROUP BY status",
            parameters,
        ).fetchall()
        for status in ("pending", "dispatched", "completed", "failed", "uncertain"):
            result[status] = next((row["count"] for row in counts if row["status"] == status), 0)
        return result

    def evaluation_inputs(self, run_id: str) -> list[dict[str, Any]]:
        """Load fixed-denominator inputs through the evaluator role's oracle join."""
        rows = self._connection.execute(
            """
            SELECT t.tenant_id, t.run_id, t.task_id, t.status AS task_status,
                   x.document->>'amount_minor' AS amount_minor,
                   o.label, d.outcome, tc.document AS thresholds,
                   t.trace_id, t.span_id AS task_span_id,
                   COALESCE(a.ended_at, r.preflight_at, r.created_at) AS occurred_at,
                   r.config_id, r.bundle_id, r.preflight_code_revision,
                   c.document AS config, ch.cohort_id
            FROM reckoner.tasks t
            JOIN reckoner.runs r
              ON r.tenant_id = t.tenant_id AND r.run_id = t.run_id
            JOIN reckoner.transactions x
              ON x.tenant_id = t.tenant_id AND x.transaction_id = t.transaction_id
            JOIN oracle.oracle_labels o
              ON o.tenant_id = t.tenant_id AND o.transaction_id = t.transaction_id
            JOIN reckoner.run_configs c
              ON c.tenant_id = r.tenant_id AND c.config_id = r.config_id
            JOIN reckoner.threshold_configs tc
              ON tc.tenant_id = c.tenant_id AND tc.config_id = c.threshold_config_id
            JOIN reckoner.cohort_members cm
              ON cm.tenant_id = t.tenant_id AND cm.transaction_id = t.transaction_id
            JOIN reckoner.cohorts ch
              ON ch.tenant_id = cm.tenant_id AND ch.cohort_id = cm.cohort_id
             AND ch.purpose = r.purpose AND ch.bundle_id = r.bundle_id
            LEFT JOIN reckoner.decisions d
              ON d.tenant_id = t.tenant_id AND d.run_id = t.run_id AND d.task_id = t.task_id
            LEFT JOIN reckoner.attempts a
              ON a.tenant_id = t.tenant_id AND a.run_id = t.run_id AND a.task_id = t.task_id
            WHERE t.run_id = %s
            ORDER BY t.tenant_id, t.task_id
            """,
            (run_id,),
        ).fetchall()
        if not rows:
            raise ValueError("unknown run identity")
        result = []
        for row in rows:
            document = dict(row)
            parameters = document.pop("thresholds")["parameters"]
            document["amount_minor"] = int(document["amount_minor"])
            document["review_cost"] = Decimal(parameters["review_cost"])
            document["margin_rate"] = Decimal(parameters["margin_rate"])
            document["occurred_at"] = document["occurred_at"].isoformat()
            result.append(document)
        return result

    def persist_evaluation(self, row: dict[str, Any], document: dict[str, Any]) -> None:
        """Insert-or-compare one evaluation and its exact evaluator-only OTLP bytes."""
        from reckoner.contracts import content_id
        from reckoner.telemetry.events import store_evaluation_span

        evaluation_id = content_id(
            {
                "tenant_id": row["tenant_id"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "evaluator_version": document["evaluator_version"],
            }
        )
        stored = _plain(document) | {
            "label_class": row["label"],
            "rate_contributions": {
                "completion": {
                    "numerator": int(row["task_status"] == "completed"),
                    "denominator": 1,
                },
                "correctness": {"numerator": int(document["correct"] is True), "denominator": 1},
                "false_positive": {
                    "numerator": int(
                        row["outcome"] == "auto-decline" and row["label"] == "legitimate"
                    ),
                    "denominator": int(row["label"] == "legitimate"),
                },
                "missed_fraud": {
                    "numerator": int(row["outcome"] == "auto-approve" and row["label"] == "fraud"),
                    "denominator": int(row["label"] == "fraud"),
                },
                "escalation": {"numerator": int(row["outcome"] == "escalate"), "denominator": 1},
            },
        }
        with self._connection.transaction():
            cursor = self._connection.cursor()
            cursor.execute(
                """
                INSERT INTO reckoner.evaluations
                  (tenant_id, run_id, task_id, evaluation_id, correct, document)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, run_id, task_id, evaluation_id) DO NOTHING
                """,
                (
                    row["tenant_id"],
                    row["run_id"],
                    row["task_id"],
                    evaluation_id,
                    document["correct"],
                    Jsonb(stored),
                ),
            )
            existing = cursor.execute(
                """
                SELECT correct, document FROM reckoner.evaluations
                WHERE tenant_id = %s AND run_id = %s AND task_id = %s AND evaluation_id = %s
                """,
                (row["tenant_id"], row["run_id"], row["task_id"], evaluation_id),
            ).fetchone()
            if existing != {"correct": document["correct"], "document": stored}:
                raise ValueError("immutable evaluation identity mismatch")
            store_evaluation_span(cursor, row=row, result=stored, evaluation_id=evaluation_id)

    def report_snapshot(self, run_id: str) -> dict[str, Any]:
        """Load evaluator-only report inputs without returning raw oracle records."""
        context = self.run_context(run_id)
        runs = self._connection.execute(
            """
            SELECT r.*, c.cohort_id
            FROM reckoner.runs r
            JOIN reckoner.cohorts c
              ON c.tenant_id = r.tenant_id AND c.bundle_id = r.bundle_id
             AND c.purpose = r.purpose
            WHERE r.run_id = %s ORDER BY r.tenant_id
            """,
            (run_id,),
        ).fetchall()
        tasks = self._connection.execute(
            """
            SELECT t.tenant_id, t.task_id, t.status, label.label AS label_class
            FROM reckoner.tasks t
            JOIN LATERAL (
              SELECT o.label FROM oracle.oracle_labels o
              WHERE o.tenant_id = t.tenant_id AND o.transaction_id = t.transaction_id
              ORDER BY o.oracle_version DESC LIMIT 1
            ) label ON true
            WHERE t.run_id = %s ORDER BY t.tenant_id, t.task_id
            """,
            (run_id,),
        ).fetchall()
        attempts = self._connection.execute(
            "SELECT tenant_id, task_id, call_id, status, actual_cost, maximum_cost, duration_ms "
            "FROM reckoner.attempts WHERE run_id = %s ORDER BY tenant_id, task_id, call_id",
            (run_id,),
        ).fetchall()
        evaluations = self._connection.execute(
            """
            SELECT e.tenant_id, e.task_id, d.outcome, e.document
            FROM reckoner.evaluations e
            LEFT JOIN reckoner.decisions d
              ON d.tenant_id = e.tenant_id AND d.run_id = e.run_id AND d.task_id = e.task_id
            WHERE e.run_id = %s ORDER BY e.tenant_id, e.task_id
            """,
            (run_id,),
        ).fetchall()
        exports = self._connection.execute(
            """
            SELECT producer, status, event_id, task_id
            FROM (
              SELECT producer, status, event_id, task_id
              FROM reckoner.runner_telemetry_outbox WHERE run_id = %s
              UNION ALL
              SELECT producer, status, event_id, task_id
              FROM reckoner.evaluator_telemetry_outbox WHERE run_id = %s
            ) evidence
            ORDER BY producer, event_id
            """,
            (run_id, run_id),
        ).fetchall()
        statuses = {run["status"] for run in runs}
        status = "complete" if statuses == {"complete"} else "incomplete"
        config = context["config"]
        return {
            "run": {
                "run_id": run_id,
                "purpose": context["purpose"],
                "status": status,
                "execution_mode": context["execution_mode"],
                "provider_call_mode": context["provider_call_mode"],
                "config_id": context["config_id"],
                "bundle_id": context["bundle_id"],
                "prompt_version": config["prompt_version"],
                "model": config["model"],
                "price_table_version": config["price_table_version"],
                "threshold_config_ids": config["threshold_config_ids"],
                "cohort_ids": {run["tenant_id"]: run["cohort_id"] for run in runs},
                "code_revision": context["preflight_code_revision"],
                "created_at": min(run["created_at"] for run in runs).isoformat(),
                "preflight_at": (
                    min(run["preflight_at"] for run in runs).isoformat()
                    if all(run["preflight_at"] is not None for run in runs)
                    else None
                ),
            },
            "tenants": [run["tenant_id"] for run in runs],
            "tasks": [dict(item) for item in tasks],
            "attempts": [dict(item) for item in attempts],
            "evaluations": [
                {
                    "tenant_id": item["tenant_id"],
                    "task_id": item["task_id"],
                    "outcome": item["outcome"],
                    "label_class": item["document"]["label_class"],
                    "document": item["document"],
                }
                for item in evaluations
            ],
            "exports": [dict(item) for item in exports],
        }

    def readiness(self) -> str:
        row = self._connection.execute(
            "SELECT EXISTS (SELECT 1 FROM public.reckoner_schema_migrations "
            "WHERE version = %s) AS applied",
            (REQUIRED_MIGRATION,),
        ).fetchone()
        if row is None or not row["applied"]:
            raise ValueError("schema migration is incompatible")
        return REQUIRED_MIGRATION

    def api_run_summary(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM reckoner.api_run_summaries WHERE tenant_id = %s AND run_id = %s",
            (tenant_id, run_id),
        ).fetchone()
        if row is None:
            return None
        result = _json_safe(dict(row))
        correct_count = result.pop("correct_count")
        result["metrics"] = {
            "correctness": (
                {
                    "numerator": correct_count,
                    "denominator": result["expected_count"],
                }
                if result["evaluated_count"] == result["expected_count"]
                else None
            ),
            "completion": {
                "numerator": result["completed_count"],
                "denominator": result["expected_count"],
            },
        }
        return result

    def api_results(
        self, tenant_id: str, run_id: str, *, limit: int, offset: int
    ) -> list[dict[str, Any]] | None:
        exists = self._connection.execute(
            "SELECT 1 FROM reckoner.api_runs WHERE tenant_id = %s AND run_id = %s",
            (tenant_id, run_id),
        ).fetchone()
        if exists is None:
            return None
        rows = self._connection.execute(
            """
            SELECT task_id, transaction_id, status, decision_id, outcome,
                   requested_model, reported_model, created_at
            FROM reckoner.api_results
            WHERE tenant_id = %s AND run_id = %s
            ORDER BY task_id LIMIT %s OFFSET %s
            """,
            (tenant_id, run_id, limit, offset),
        ).fetchall()
        return [_json_safe(dict(row)) for row in rows]

    @contextmanager
    def exclusive_runner(self) -> Iterator[None]:
        acquired = self._connection.execute(
            "SELECT pg_try_advisory_lock(%s)", (RUNNER_LOCK,)
        ).fetchone()["pg_try_advisory_lock"]
        if not acquired:
            raise RunBusy("another baseline runner holds the database lock")
        try:
            yield
        finally:
            self._connection.execute("SELECT pg_advisory_unlock(%s)", (RUNNER_LOCK,))

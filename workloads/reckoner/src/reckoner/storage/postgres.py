"""Concrete PostgreSQL repository for baseline state."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from jsonschema import ValidationError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from reckoner.contracts import content_id, validate_document, validate_threshold_config
from reckoner.data.artifacts import verify_bundle
from reckoner.storage.budget import RunBusy

ROOT = Path(__file__).resolve().parents[5]
SCHEMAS = ROOT / "contracts" / "schemas"
RUNNER_LOCK = 732019101


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
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

    def create_run(self, run_id: str, purpose: str, config: dict, bundle_id: str) -> None:
        """Freeze a shared config per tenant and create every cohort task atomically."""
        if not run_id:
            raise ValueError("run ID is required")
        if purpose not in {"pilot", "baseline"}:
            raise ValueError("invalid run purpose")
        plain = _plain(config)
        try:
            validate_document(plain, SCHEMAS / "run-config-v1.schema.json")
        except ValidationError as error:
            raise ValueError("invalid run configuration") from error
        body = {key: value for key, value in plain.items() if key != "config_id"}
        if plain["config_id"] != content_id(body):
            raise ValueError("run config identity does not match contents")

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
                cursor.execute(
                    """
                    INSERT INTO reckoner.runs
                      (tenant_id, run_id, purpose, config_id, bundle_id)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, run_id) DO NOTHING
                    """,
                    (tenant_id, run_id, purpose, plain["config_id"], bundle_id),
                )
                existing = cursor.execute(
                    """
                    SELECT purpose, config_id, bundle_id FROM reckoner.runs
                    WHERE tenant_id = %s AND run_id = %s
                    """,
                    (tenant_id, run_id),
                ).fetchone()
                if existing != {
                    "purpose": purpose,
                    "config_id": plain["config_id"],
                    "bundle_id": bundle_id,
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

    def pending_tasks(self, run_id: str) -> list[dict]:
        rows = self._connection.execute(
            """
            SELECT t.tenant_id, t.run_id, t.task_id, x.document AS transaction, t.status
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
        return result

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

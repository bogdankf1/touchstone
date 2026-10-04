"""Additive v1 records; providers receive documents, never database connections."""

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from reckoner.v1.contracts import validate_v1


class V1Repository:
    def __init__(
        self,
        dsn: str,
        *,
        scorer_client=None,
        note_client=None,
        note_protocol=None,
        fixture_dispatch=False,
    ):
        self.scorer_client = scorer_client
        # Tests only: admit dispatch under fabricated fixture-labelled approvals.
        self.fixture_dispatch = fixture_dispatch is True
        self.note_client = note_client
        self.note_protocol = note_protocol
        self._connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._connection.close()

    def _insert(self, table: str, values: dict, identity: dict) -> dict:
        """Append once, comparing complete documents after any concurrent duplicate."""
        self._connection.execute(
            sql.SQL("INSERT INTO reckoner.{} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
                sql.Identifier(table),
                sql.SQL(", ").join(map(sql.Identifier, values)),
                sql.SQL(", ").join(sql.Placeholder() for _ in values),
            ),
            tuple(Jsonb(value) if isinstance(value, dict) else value for value in values.values()),
        )
        row = self._connection.execute(
            sql.SQL("SELECT document FROM reckoner.{} WHERE {}").format(
                sql.Identifier(table),
                sql.SQL(" AND ").join(
                    sql.SQL("{} = %s").format(sql.Identifier(key)) for key in identity
                ),
            ),
            tuple(identity.values()),
        ).fetchone()
        if row is None or row["document"] != values["document"]:
            raise ValueError("immutable v1 record conflict")
        return row["document"]

    def register_config(self, document: dict) -> str:
        document = validate_v1("run-config", document)
        with self._connection.transaction():
            self._insert(
                "v1_configs",
                {
                    "tenant_id": document["tenant_id"],
                    "config_id": document["config_id"],
                    "threshold_config_id": document["threshold_config_id"],
                    "workflow_version": document["workflow_version"],
                    "document": document,
                },
                {"tenant_id": document["tenant_id"], "config_id": document["config_id"]},
            )
        return document["config_id"]

    def create_run(self, manifest: dict, config_id: str, *, telemetry_mode="workflow") -> dict:
        from reckoner.v1.telemetry.events import OFFLINE_SCORING_PURPOSES

        manifest = validate_v1("experiment", manifest)
        if telemetry_mode not in {"workflow", "scoring-only"} or (
            telemetry_mode == "scoring-only" and manifest["purpose"] not in OFFLINE_SCORING_PURPOSES
        ):
            raise ValueError("explicit supported offline scoring mode required")
        if manifest["config_id"] != config_id:
            raise ValueError("manifest configuration does not match")
        with self._connection.transaction():
            result = self._insert(
                "v1_runs",
                {
                    "tenant_id": manifest["tenant_id"],
                    "run_id": manifest["run_id"],
                    "config_id": config_id,
                    "experiment_id": manifest["experiment_id"],
                    "purpose": manifest["purpose"],
                    "telemetry_mode": telemetry_mode,
                    "document": manifest,
                    "created_at": manifest["created_at"],
                },
                {"tenant_id": manifest["tenant_id"], "run_id": manifest["run_id"]},
            )
            saved_mode = self._connection.execute(
                "SELECT telemetry_mode FROM reckoner.v1_runs WHERE tenant_id=%s AND run_id=%s",
                (manifest["tenant_id"], manifest["run_id"]),
            ).fetchone()["telemetry_mode"]
            if saved_mode != telemetry_mode:
                raise ValueError("immutable telemetry mode conflict")
            for task in manifest["tasks"]:
                identity = {
                    "tenant_id": manifest["tenant_id"],
                    "run_id": manifest["run_id"],
                    "task_id": task["task_id"],
                }
                task_document = {**identity, "transaction_id": task["transaction_id"]}
                self._insert("v1_tasks", {**task_document, "document": task_document}, identity)
            from reckoner.v1.telemetry.events import (
                run_declaration,
                scoring_events,
            )
            from reckoner.v1.telemetry.outbox import enqueue_events

            config = self.workflow_document(
                "v1_configs", {"tenant_id": manifest["tenant_id"], "config_id": config_id}
            )
            run = {"manifest": manifest, "config": config, "telemetry_mode": telemetry_mode}
            enqueue_events(self, [run_declaration(run)])
            if telemetry_mode == "scoring-only":
                for task in manifest["tasks"]:
                    enqueue_events(self, scoring_events(run, task))
        return result

    def task(self, tenant_id: str, run_id: str, task_id: str) -> dict:
        row = self._connection.execute(
            "SELECT task.tenant_id, task.run_id, task.task_id, task.transaction_id, task.status, "
            "run.config_id, transaction.document AS transaction "
            "FROM reckoner.v1_tasks task JOIN reckoner.v1_runs run "
            "ON (run.tenant_id, run.run_id) = (task.tenant_id, task.run_id) "
            "JOIN reckoner.transactions transaction "
            "ON (transaction.tenant_id, transaction.transaction_id) "
            "= (task.tenant_id, task.transaction_id) "
            "WHERE task.tenant_id = %s AND task.run_id = %s AND task.task_id = %s",
            (tenant_id, run_id, task_id),
        ).fetchone()
        if row is None:
            raise LookupError("v1 task not found")
        return row

    def persist_evidence(self, document: dict) -> str:
        document = validate_v1("evidence", document)
        with self._connection.transaction():
            self._insert(
                "v1_evidence",
                {
                    "tenant_id": document["tenant_id"],
                    "evidence_id": document["evidence_id"],
                    "transaction_id": document["transaction_id"],
                    "query_time": document["query_time"],
                    "coverage_status": document["coverage"]["status"],
                    "document": document,
                },
                {"tenant_id": document["tenant_id"], "evidence_id": document["evidence_id"]},
            )
        return document["evidence_id"]

    def persist_decision(self, document: dict) -> dict:
        document = validate_v1("decision", document)
        identity = {key: document[key] for key in ("tenant_id", "run_id", "task_id")}
        with self._connection.transaction():
            # Serialize one task's terminal writes, including after a lost response.
            self._connection.execute(
                "SELECT task_id FROM reckoner.v1_tasks "
                "WHERE tenant_id=%s AND run_id=%s AND task_id=%s FOR UPDATE",
                tuple(identity.values()),
            ).fetchone()
            values = {
                key: document[key]
                for key in (
                    "tenant_id",
                    "run_id",
                    "task_id",
                    "transaction_id",
                    "decision_id",
                    "config_id",
                    "evidence_id",
                    "outcome",
                    "scorer_status",
                    "call_id",
                    "completed_at",
                )
            }
            result = self._insert("v1_decisions", {**values, "document": document}, identity)
            if document["outcome"] == "escalate":
                case = {
                    "tenant_id": document["tenant_id"],
                    "case_id": document["decision_id"],
                    "decision_id": document["decision_id"],
                }
                self._insert(
                    "v1_cases",
                    {**case, "document": case},
                    {"tenant_id": case["tenant_id"], "case_id": case["case_id"]},
                )
            from reckoner.v1.notes.lifecycle import declare_note

            declare_note(self, document)
            from reckoner.v1.telemetry.events import measurement_events
            from reckoner.v1.telemetry.outbox import enqueue_events

            run = self._connection.execute(
                "SELECT r.document AS manifest,c.document AS config FROM reckoner.v1_runs r "
                "JOIN reckoner.v1_configs c USING(tenant_id,config_id) "
                "WHERE r.tenant_id=%s AND r.run_id=%s",
                (document["tenant_id"], document["run_id"]),
            ).fetchone()
            enqueue_events(self, measurement_events(run, {"decision": document}, []))
            self._connection.execute(
                "UPDATE reckoner.v1_tasks SET status='completed' "
                "WHERE tenant_id=%s AND run_id=%s AND task_id=%s",
                tuple(identity.values()),
            )
        return result

    def workflow_document(self, table: str, identity: dict) -> dict | None:
        if table not in {
            "v1_note_work",
            "v1_note_results",
            "v1_workflow_runs",
            "v1_workflow_tasks",
            "v1_configs",
            "v1_evidence",
            "v1_decisions",
            "threshold_configs",
        }:
            raise ValueError("unsupported workflow document")
        row = self._connection.execute(
            sql.SQL("SELECT document FROM reckoner.{} WHERE {}").format(
                sql.Identifier(table),
                sql.SQL(" AND ").join(
                    sql.SQL("{}=%s").format(sql.Identifier(key)) for key in identity
                ),
            ),
            tuple(identity.values()),
        ).fetchone()
        return row["document"] if row else None

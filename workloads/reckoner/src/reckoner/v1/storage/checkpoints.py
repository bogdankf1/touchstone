"""Tenant-scoped synchronous LangGraph BaseCheckpointSaver for PostgreSQL.

Full supported checkpoint payloads use LangGraph's typed serializer. Runtime
clients and source documents are supplied separately via graph runtime context.
"""

from threading import RLock

import psycopg
from langgraph.checkpoint.base import WRITES_IDX_MAP, BaseCheckpointSaver, CheckpointTuple
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from reckoner.contracts import content_id


def task_config(task: dict) -> dict:
    identity = {key: task[key] for key in ("tenant_id", "run_id", "task_id")}
    return {"configurable": {**identity, "thread_id": content_id(list(identity.values()))}}


class PostgresCheckpointer(BaseCheckpointSaver):
    def __init__(self, dsn: str, tenant_id: str):
        super().__init__()
        self.tenant_id = tenant_id
        self.connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        self.lock = RLock()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def _scope(self, config):
        values = config["configurable"]
        if values.get("tenant_id") != self.tenant_id:
            raise ValueError("checkpoint tenant mismatch")
        expected = task_config(values)["configurable"]["thread_id"]
        if values.get("thread_id") != expected:
            raise ValueError("checkpoint task identity mismatch")
        return (
            self.tenant_id,
            values["run_id"],
            values["task_id"],
            values.get("checkpoint_ns", ""),
        )

    def _config(self, scope, checkpoint_id):
        result = task_config(dict(zip(("tenant_id", "run_id", "task_id"), scope[:3], strict=True)))
        result["configurable"].update(checkpoint_ns=scope[3], checkpoint_id=checkpoint_id)
        return result

    def _validate_values(self, scope, values):
        row = self.connection.execute(
            "SELECT document FROM reckoner.v1_workflow_tasks "
            "WHERE tenant_id=%s AND run_id=%s AND task_id=%s",
            scope[:3],
        ).fetchone()
        if row is None:
            raise ValueError("checkpoint requires pinned task binding")
        binding = row["document"]
        for key in (
            "tenant_id",
            "run_id",
            "task_id",
            "transaction_id",
            "config_id",
            "evidence_id",
            "protocol_id",
            "calibration_id",
            "started_at",
        ):
            if key in values and values[key] != binding[key]:
                raise ValueError("checkpoint identity differs from pinned task")
        if isinstance(values.get("__start__"), dict):
            self._validate_values(scope, values["__start__"])

    def get_tuple(self, config):
        scope = self._scope(config)
        checkpoint_id = config["configurable"].get("checkpoint_id")
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM reckoner.v1_checkpoints WHERE tenant_id=%s AND run_id=%s "
                "AND task_id=%s AND checkpoint_ns=%s "
                "AND (%s::text IS NULL OR checkpoint_id=%s) ORDER BY checkpoint_id DESC LIMIT 1",
                (*scope, checkpoint_id, checkpoint_id),
            ).fetchone()
            if row is None:
                return None
            writes = self.connection.execute(
                "SELECT * FROM reckoner.v1_checkpoint_writes WHERE tenant_id=%s AND run_id=%s "
                "AND task_id=%s AND checkpoint_ns=%s AND checkpoint_id=%s "
                "ORDER BY node_task_id,write_index",
                (*scope, row["checkpoint_id"]),
            ).fetchall()
        return CheckpointTuple(
            config=self._config(scope, row["checkpoint_id"]),
            checkpoint=self.serde.loads_typed((row["encoding"], bytes(row["payload"]))),
            metadata=row["metadata"],
            parent_config=self._config(scope, row["parent_id"]) if row["parent_id"] else None,
            pending_writes=[
                (
                    w["node_task_id"],
                    w["channel"],
                    self.serde.loads_typed((w["encoding"], bytes(w["payload"]))),
                )
                for w in writes
            ],
        )

    def list(self, config, *, filter=None, before=None, limit=None):
        if config is None:
            raise ValueError("task-scoped checkpoint configuration required")
        scope = self._scope(config)
        before_id = None
        if before:
            if self._scope(before) != scope:
                raise ValueError("checkpoint task identity mismatch")
            before_id = before["configurable"].get("checkpoint_id")
        with self.lock:
            rows = self.connection.execute(
                "SELECT checkpoint_id FROM reckoner.v1_checkpoints "
                "WHERE tenant_id=%s AND run_id=%s AND task_id=%s AND checkpoint_ns=%s "
                "AND (%s::text IS NULL OR checkpoint_id < %s) AND metadata @> %s "
                "ORDER BY checkpoint_id DESC LIMIT %s",
                (*scope, before_id, before_id, Jsonb(filter or {}), limit),
            ).fetchall()
        for row in rows:
            yield self.get_tuple(self._config(scope, row["checkpoint_id"]))

    def put(self, config, checkpoint, metadata, new_versions):
        scope = self._scope(config)
        encoding, payload = self.serde.dumps_typed(checkpoint)
        # Never merge configurable/runtime values into persisted metadata.
        safe_metadata = {
            key: metadata[key] for key in ("source", "step", "parents") if key in metadata
        }
        with self.lock:
            self._validate_values(scope, checkpoint["channel_values"])
            self.connection.execute(
                "INSERT INTO reckoner.v1_checkpoints "
                "(tenant_id,run_id,task_id,checkpoint_ns,checkpoint_id,parent_id,"
                "encoding,payload,metadata) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    *scope,
                    checkpoint["id"],
                    config["configurable"].get("checkpoint_id"),
                    encoding,
                    payload,
                    Jsonb(safe_metadata),
                ),
            )
        return self._config(scope, checkpoint["id"])

    def put_writes(self, config, writes, task_id, task_path=""):
        scope = self._scope(config)
        with self.lock, self.connection.transaction():
            self._validate_values(scope, dict(writes))
            for index, (channel, value) in enumerate(writes):
                # Provider errors never become unbounded exception text in checkpoints.
                if channel == "__error__":
                    value = "node_failed"
                encoding, payload = self.serde.dumps_typed(value)
                special = channel in WRITES_IDX_MAP
                conflict = (
                    "DO UPDATE SET channel=EXCLUDED.channel,encoding=EXCLUDED.encoding,"
                    "payload=EXCLUDED.payload"
                    if special
                    else "DO NOTHING"
                )
                self.connection.execute(
                    "INSERT INTO reckoner.v1_checkpoint_writes "
                    "(tenant_id,run_id,task_id,checkpoint_ns,checkpoint_id,node_task_id,"
                    "write_index,channel,encoding,payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (tenant_id,run_id,task_id,checkpoint_ns,checkpoint_id,"
                    "node_task_id,write_index) " + conflict,
                    (
                        *scope,
                        config["configurable"]["checkpoint_id"],
                        task_id,
                        WRITES_IDX_MAP.get(channel, index),
                        channel,
                        encoding,
                        payload,
                    ),
                )

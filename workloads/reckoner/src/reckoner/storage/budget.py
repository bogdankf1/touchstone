"""Serialized provider budget accounting."""

from __future__ import annotations

import uuid
from decimal import Decimal

from psycopg.types.json import Jsonb

GLOBAL_LIMIT = Decimal("10.00")
PILOT_LIMIT = Decimal("1.00")
ACCOUNTING_LOCK = 732019102


class BudgetExceeded(RuntimeError):
    pass


class RunBusy(RuntimeError):
    pass


class BudgetLedger:
    """Serialize reservations and settlement across every run and tenant."""

    def __init__(self, repo):
        self._connection = repo._connection

    @staticmethod
    def _validate_maximum(maximum: Decimal) -> Decimal:
        if not isinstance(maximum, Decimal) or not maximum.is_finite() or maximum < 0:
            raise ValueError("maximum reservation must be a nonnegative decimal")
        return maximum

    @staticmethod
    def _spent(cursor, purpose: str | None = None) -> Decimal:
        where = "WHERE purpose = %s" if purpose else ""
        parameters = (purpose,) if purpose else ()
        row = cursor.execute(
            f"""
            SELECT COALESCE(SUM(
              CASE WHEN status = 'settled' THEN actual_cost ELSE maximum_cost END
            ), 0) AS spent
            FROM reckoner.budget_entries
            {where}
            """,
            parameters,
        ).fetchone()
        return Decimal(row["spent"])

    def reserve(self, task: dict, maximum: Decimal) -> dict:
        maximum = self._validate_maximum(maximum)
        tenant_id = task["tenant_id"]
        run_id = task["run_id"]
        task_id = task["task_id"]
        with self._connection.transaction():
            cursor = self._connection.cursor()
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
            current = cursor.execute(
                """
                SELECT t.status, r.purpose
                FROM reckoner.tasks t
                JOIN reckoner.runs r
                  ON r.tenant_id = t.tenant_id AND r.run_id = t.run_id
                WHERE t.tenant_id = %s AND t.run_id = %s AND t.task_id = %s
                FOR UPDATE OF t
                """,
                (tenant_id, run_id, task_id),
            ).fetchone()
            if current is None or current["status"] != "pending":
                raise ValueError("task is not pending")
            if (
                task.get("reservation_cost") is not None
                and Decimal(task["reservation_cost"]) != maximum
            ):
                raise ValueError("reservation does not match validated preflight")

            has_overage = cursor.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM reckoner.budget_entries
                  WHERE actual_cost > maximum_cost
                ) AS has_overage
                """
            ).fetchone()["has_overage"]
            if has_overage:
                raise BudgetExceeded("provider cost overage requires reconciliation")

            global_spent = self._spent(cursor)
            if global_spent >= GLOBAL_LIMIT or global_spent + maximum > GLOBAL_LIMIT:
                raise BudgetExceeded("global provider budget exceeded")
            if current["purpose"] == "pilot":
                pilot_spent = self._spent(cursor, "pilot")
                if pilot_spent >= PILOT_LIMIT or pilot_spent + maximum > PILOT_LIMIT:
                    raise BudgetExceeded("pilot provider budget exceeded")

            call_id = str(uuid.uuid4())
            cursor.execute(
                """
                INSERT INTO reckoner.attempts
                  (tenant_id, run_id, task_id, call_id, status, maximum_cost,
                   requested_model, request_document, request_sha256, trace_id, span_id)
                VALUES (%s, %s, %s, %s, 'dispatched', %s, %s, %s, %s, %s, %s)
                """,
                (
                    tenant_id,
                    run_id,
                    task_id,
                    call_id,
                    maximum,
                    task["request"].get("model") if task.get("request") is not None else None,
                    Jsonb(task["request"]) if task.get("request") is not None else None,
                    task.get("request_sha256"),
                    task.get("trace_id"),
                    task.get("provider_span_id"),
                ),
            )
            cursor.execute(
                """
                INSERT INTO reckoner.budget_entries
                  (tenant_id, run_id, task_id, call_id, purpose, maximum_cost, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'reserved')
                """,
                (tenant_id, run_id, task_id, call_id, current["purpose"], maximum),
            )
            cursor.execute(
                """
                UPDATE reckoner.tasks SET status = 'dispatched'
                WHERE tenant_id = %s AND run_id = %s AND task_id = %s
                """,
                (tenant_id, run_id, task_id),
            )
        return {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "task_id": task_id,
            "call_id": call_id,
            "maximum": maximum,
            "status": "reserved",
        }

    def settle(self, call_id: str, actual: Decimal | None, usage: dict | None) -> None:
        if (actual is None) != (usage is None):
            raise ValueError("actual cost and usage must both be present or absent")
        if actual is not None and (
            not isinstance(actual, Decimal) or not actual.is_finite() or actual < 0
        ):
            raise ValueError("actual cost must be a nonnegative decimal")
        with self._connection.transaction():
            cursor = self._connection.cursor()
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
            entry = cursor.execute(
                "SELECT * FROM reckoner.budget_entries WHERE call_id = %s FOR UPDATE",
                (call_id,),
            ).fetchone()
            if entry is None:
                raise ValueError("unknown call identity")
            if entry["status"] == "settled":
                if entry["actual_cost"] == actual and entry["usage"] == usage:
                    return
                raise ValueError("conflicting settlement")
            if entry["status"] == "uncertain" and actual is None:
                return

            if actual is None:
                cursor.execute(
                    """
                    UPDATE reckoner.budget_entries
                    SET status = 'uncertain', settled_at = now()
                    WHERE call_id = %s
                    """,
                    (call_id,),
                )
                cursor.execute(
                    "UPDATE reckoner.attempts SET status = 'uncertain', settled_at = now() "
                    "WHERE call_id = %s",
                    (call_id,),
                )
                cursor.execute(
                    """
                    UPDATE reckoner.tasks SET status = 'uncertain'
                    WHERE tenant_id = %s AND run_id = %s AND task_id = %s
                    """,
                    (entry["tenant_id"], entry["run_id"], entry["task_id"]),
                )
                return

            cursor.execute(
                """
                UPDATE reckoner.budget_entries
                SET status = 'settled', actual_cost = %s, usage = %s, settled_at = now()
                WHERE call_id = %s
                """,
                (actual, Jsonb(usage), call_id),
            )
            cursor.execute(
                """
                UPDATE reckoner.attempts
                SET status = 'responded', actual_cost = %s, usage = %s, settled_at = now()
                WHERE call_id = %s
                """,
                (actual, Jsonb(usage), call_id),
            )

    def remaining(self) -> Decimal:
        spent = self._spent(self._connection.cursor())
        return max(Decimal(0), GLOBAL_LIMIT - spent)

    def pilot_remaining(self) -> Decimal:
        spent = self._spent(self._connection.cursor(), "pilot")
        return max(Decimal(0), PILOT_LIMIT - spent)

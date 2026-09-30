"""Provider-wide protocol envelopes, attempt allocations, immutable settlements."""

from decimal import Decimal

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from reckoner.contracts import content_id
from reckoner.storage.budget import ACCOUNTING_LOCK, BudgetExceeded, BudgetLedger
from reckoner.v1.providers.jev import valid_usage

PROVIDERS = {"typesafe": "jev-1.13.0", "anthropic": "anthropic/claude-haiku-4-5-20251001"}


def validate_protocol(protocol):
    fields = {
        "tenant_id",
        "run_id",
        "provider",
        "purpose",
        "model",
        "input_token_ceiling",
        "max_output_tokens",
        "maximum_attempts",
        "usd_cap",
        "approved",
        "tasks",
        "protocol_id",
    }
    if set(protocol) != fields or protocol["approved"] is not True:
        raise ValueError("an exact approved paid-run protocol is required")
    if protocol["protocol_id"] != content_id(
        {k: v for k, v in protocol.items() if k != "protocol_id"}
    ):
        raise ValueError("protocol content identity mismatch")
    if PROVIDERS.get(protocol["provider"]) != protocol["model"]:
        raise ValueError("protocol model is not pinned")
    for key in ("input_token_ceiling", "max_output_tokens", "maximum_attempts"):
        if type(protocol[key]) is not int or protocol[key] <= 0:
            raise ValueError("protocol bounds must be positive integers")
    if protocol["maximum_attempts"] > 3 or protocol["input_token_ceiling"] > 32000:
        raise ValueError("protocol exceeds supported bounds")
    if not isinstance(protocol["tasks"], list) or not protocol["tasks"]:
        raise ValueError("protocol must list exact cases")
    for task in protocol["tasks"]:
        if set(task) != {"task_id", "transaction_id", "request_sha256"} or any(
            not isinstance(v, str) or not v for v in task.values()
        ):
            raise ValueError("protocol must list exact requests")
    if len({t["task_id"] for t in protocol["tasks"]}) != len(protocol["tasks"]) or len(
        {t["transaction_id"] for t in protocol["tasks"]}
    ) != len(protocol["tasks"]):
        raise ValueError("protocol cases must be unique")
    maximum = Decimal(protocol["usd_cap"])
    BudgetLedger._validate_maximum(maximum)
    return protocol


class ProviderBudget:
    def __init__(self, connection):
        self._connection = connection

    def _cursor(self):
        return self._connection.cursor(row_factory=dict_row)

    @staticmethod
    def _legacy(cursor):
        rows = cursor.execute(
            "SELECT tenant_id,run_id,task_id,call_id,status,maximum_cost,"
            "actual_cost,usage FROM reckoner.budget_entries ORDER BY call_id"
        ).fetchall()
        serial = [
            {k: str(v) if isinstance(v, Decimal) else v for k, v in row.items()} for row in rows
        ]
        return rows, content_id(serial)

    def verify_legacy(self):
        """Owner gate: hash actual restored ledger rows; never manufacture a balance."""
        with self._connection.transaction():
            cursor = self._cursor()
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
            rows, digest = self._legacy(cursor)
            if (
                len(rows) != 1040
                or any(r["status"] != "settled" for r in rows)
                or sum(r["actual_cost"] for r in rows) != Decimal(".493151")
            ):
                raise BudgetExceeded("original legacy ledger must be restored and verified")
            for tenant in sorted({r["tenant_id"] for r in rows}):
                document = {
                    "tenant_id": tenant,
                    "provider": "anthropic",
                    "call_count": len(rows),
                    "settled_cost": ".493151",
                    "ledger_sha256": digest,
                }
                cursor.execute(
                    "INSERT INTO reckoner.v1_legacy_provenance "
                    "(tenant_id,provenance_id,document) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (tenant, content_id(document), Jsonb(document)),
                )
            return digest

    def _check_legacy(self, cursor):
        rows, digest = self._legacy(cursor)
        provenance = cursor.execute("SELECT document FROM reckoner.v1_legacy_provenance").fetchall()
        if (
            not rows
            or not provenance
            or any(r["document"]["ledger_sha256"] != digest for r in provenance)
        ):
            raise BudgetExceeded(
                "verified legacy ledger provenance is required; legacy dispatch must stop"
            )

    @staticmethod
    def _allocations(cursor, protocol_id):
        return cursor.execute(
            "SELECT COALESCE(SUM(COALESCE(s.cost,c.maximum_cost)),0) AS used "
            "FROM reckoner.v1_provider_calls c LEFT JOIN reckoner.v1_settlements s "
            "ON s.call_id=c.call_id AND s.status='settled' WHERE c.protocol_id=%s",
            (protocol_id,),
        ).fetchone()["used"]

    def _liability(self, cursor, provider):
        protocols = cursor.execute(
            "SELECT p.*, cl.protocol_id IS NOT NULL AS closed "
            "FROM reckoner.v1_protocols p LEFT JOIN reckoner.v1_protocol_closures cl "
            "USING(protocol_id) WHERE p.provider=%s",
            (provider,),
        ).fetchall()
        spent = sum(
            self._allocations(cursor, p["protocol_id"])
            if p["closed"]
            else max(p["maximum_cost"], self._allocations(cursor, p["protocol_id"]))
            for p in protocols
        )
        if provider == "anthropic":
            spent += BudgetLedger._spent(cursor)
        return Decimal(spent)

    def remaining(self, provider):
        with self._connection.transaction():
            cursor = self._cursor()
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
            return max(Decimal(0), Decimal(10) - self._liability(cursor, provider))

    def reserve(self, call: dict, maximum: Decimal, protocol: dict) -> dict:
        validate_protocol(protocol)
        BudgetLedger._validate_maximum(maximum)
        for key in (
            "tenant_id",
            "run_id",
            "provider",
            "purpose",
            "model",
            "input_token_ceiling",
            "max_output_tokens",
        ):
            if call.get(key) != protocol[key]:
                raise ValueError("call does not match protocol")
        case = {
            "task_id": call["task_id"],
            "transaction_id": call["transaction_id"],
            "request_sha256": call["request_sha256"],
        }
        if case not in protocol["tasks"]:
            raise ValueError("request is outside approved protocol")
        reservation = {**call, "protocol_id": protocol["protocol_id"], "maximum": str(maximum)}
        with self._connection.transaction():
            cursor = self._cursor()
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
            existing = cursor.execute(
                "SELECT document FROM reckoner.v1_provider_calls WHERE call_id=%s",
                (call["call_id"],),
            ).fetchone()
            if existing:
                if existing["document"] != reservation:
                    raise ValueError("conflicting reservation")
                return existing["document"]
            if call["provider"] == "anthropic":
                self._check_legacy(cursor)
            overage = cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM reckoner.v1_provider_calls c "
                "JOIN reckoner.v1_settlements s USING(call_id) WHERE c.provider=%s AND "
                "(s.cost > c.maximum_cost OR (s.usage->>'input_tokens')::numeric "
                "> (c.document->>'input_token_ceiling')::numeric OR "
                "(s.usage->>'output_tokens')::numeric > "
                "(c.document->>'max_output_tokens')::numeric)) "
                "AS overage",
                (call["provider"],),
            ).fetchone()["overage"]
            if call["provider"] == "anthropic":
                overage = (
                    overage
                    or cursor.execute(
                        "SELECT EXISTS(SELECT 1 FROM reckoner.budget_entries "
                        "WHERE actual_cost>maximum_cost) AS overage"
                    ).fetchone()["overage"]
                )
            if overage:
                raise BudgetExceeded("provider overage requires reconciliation")
            current = cursor.execute(
                "SELECT document FROM reckoner.v1_protocols WHERE protocol_id=%s",
                (protocol["protocol_id"],),
            ).fetchone()
            if not current:
                cap = Decimal(protocol["usd_cap"])
                if self._liability(cursor, call["provider"]) + cap > Decimal(10):
                    raise BudgetExceeded("provider-wide budget exceeded")
                cursor.execute(
                    "INSERT INTO reckoner.v1_protocols "
                    "(tenant_id,protocol_id,provider,run_id,maximum_cost,document) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        call["tenant_id"],
                        protocol["protocol_id"],
                        call["provider"],
                        call["run_id"],
                        cap,
                        Jsonb(protocol),
                    ),
                )
            elif current["document"] != protocol:
                raise ValueError("conflicting protocol")
            if cursor.execute(
                "SELECT 1 FROM reckoner.v1_protocol_closures WHERE protocol_id=%s",
                (protocol["protocol_id"],),
            ).fetchone():
                raise BudgetExceeded("protocol is closed")
            count = cursor.execute(
                "SELECT count(*) AS n FROM reckoner.v1_provider_calls "
                "WHERE protocol_id=%s AND task_id=%s",
                (protocol["protocol_id"], call["task_id"]),
            ).fetchone()["n"]
            if count >= protocol["maximum_attempts"]:
                raise BudgetExceeded("protocol attempts exhausted")
            if self._allocations(cursor, protocol["protocol_id"]) + maximum > Decimal(
                protocol["usd_cap"]
            ):
                raise BudgetExceeded("protocol budget exceeded")
            cursor.execute(
                "INSERT INTO reckoner.v1_provider_calls "
                "(tenant_id,call_id,protocol_id,provider,run_id,task_id,purpose,"
                "maximum_cost,document) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    call["tenant_id"],
                    call["call_id"],
                    protocol["protocol_id"],
                    call["provider"],
                    call["run_id"],
                    call["task_id"],
                    call["purpose"],
                    maximum,
                    Jsonb(reservation),
                ),
            )
        return reservation

    def settle(self, call_id: str, usage: dict | None, cost: Decimal | None) -> None:
        if (usage is None) != (cost is None):
            raise ValueError("usage and cost must be present together")
        valid_usage(usage)
        if cost is not None:
            BudgetLedger._validate_maximum(cost)
        status = "uncertain" if cost is None else "settled"
        with self._connection.transaction():
            cursor = self._cursor()
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
            call = cursor.execute(
                "SELECT tenant_id FROM reckoner.v1_provider_calls WHERE call_id=%s", (call_id,)
            ).fetchone()
            if call is None:
                raise ValueError("unknown call identity")
            previous = cursor.execute(
                "SELECT * FROM reckoner.v1_settlements WHERE call_id=%s AND status=%s",
                (call_id, status),
            ).fetchone()
            if previous:
                if previous["usage"] == usage and previous["cost"] == cost:
                    return
                raise ValueError("conflicting settlement")
            cursor.execute(
                "INSERT INTO reckoner.v1_settlements (tenant_id,call_id,status,usage,cost) "
                "VALUES (%s,%s,%s,%s,%s)",
                (
                    call["tenant_id"],
                    call_id,
                    status,
                    Jsonb(usage) if usage is not None else None,
                    cost,
                ),
            )

    def close(self, protocol_id):
        """Release unused envelope only; every unresolved call keeps its maximum."""
        with self._connection.transaction():
            cursor = self._cursor()
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
            cursor.execute(
                "INSERT INTO reckoner.v1_protocol_closures (tenant_id,protocol_id) "
                "SELECT tenant_id,protocol_id FROM reckoner.v1_protocols WHERE protocol_id=%s "
                "ON CONFLICT DO NOTHING",
                (protocol_id,),
            )

"""Provider-wide protocol envelopes, attempt allocations, immutable settlements.

Consent is never a field inside a protocol. An owner records an external approval
bound to the exact protocol SHA-256 (`authorize`), which also reserves each dispatch
envelope's full maximum before any attempt. `reserve` admits attempts only inside
an envelope that was recorded that way.
"""

import json
import re
from datetime import datetime
from decimal import Decimal

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from reckoner.contracts import content_id
from reckoner.storage.budget import ACCOUNTING_LOCK, BudgetExceeded, BudgetLedger
from reckoner.v1 import pricing
from reckoner.v1.providers.jev import valid_usage

# Pinned provider models come from the single dated pricing source.
PROVIDERS = {provider: entry["model"] for provider, entry in pricing.PUBLISHED.items()}
PROVIDER_CAP = Decimal(10)
HEX = re.compile(r"^[a-f0-9]{64}$")
MONEY = re.compile(r"^([0-9]+(\.[0-9]+)?|\.[0-9]+)$")
APPROVAL_SCHEMA = "reckoner-protocol-approval-v1"
FIXTURE_SCHEMA = "fabricated-test-protocol"
APPROVAL_FIELDS = {
    "schema_version",
    "approval_id",
    "protocol_sha256",
    "approver",
    "approved_at",
    "scope",
    "owner_statement",
}
SCOPE_FIELDS = {"provider", "model", "purpose", "case_count", "maximum_attempts", "usd_cap"}
DERIVATION_FIELDS = {
    "policy_version",
    "stage",
    "builder",
    "parent_stage",
    "template_sha256",
    "library",
    "library_version",
}


def money(value) -> Decimal:
    """A positive exact decimal string; never a wildcard, float or remaining balance."""
    if not isinstance(value, str) or not MONEY.match(value):
        raise ValueError("USD amounts must be exact positive decimal strings")
    amount = Decimal(value)
    if amount <= 0:
        raise ValueError("USD amounts must be positive")
    return amount


def _text(value, limit=256):
    return isinstance(value, str) and value.strip() != "" and len(value) <= limit


def validate_protocol(protocol):
    """Structural dispatch protocol (one tenant/run/provider). Consent lives elsewhere."""
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
        "tasks",
        "protocol_id",
    }
    if not isinstance(protocol, dict) or set(protocol) - {"derivation", "attempt_maximum_usd"} != (
        fields
    ):
        raise ValueError("an exact paid-run dispatch protocol is required")
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
    derivation = protocol.get("derivation")
    if derivation is not None and (
        not isinstance(derivation, dict)
        or set(derivation) != DERIVATION_FIELDS
        or not all(_text(v) for v in derivation.values())
        or derivation["policy_version"] != "derived-request-v1"
        or not HEX.match(derivation["template_sha256"])
        or protocol["provider"] != "anthropic"
        or protocol["maximum_attempts"] != 1
    ):
        raise ValueError("unsupported derived-request policy")
    for task in protocol["tasks"]:
        if (
            not isinstance(task, dict)
            or set(task) != {"task_id", "transaction_id", "request_sha256"}
            or not _text(task["task_id"])
            or not _text(task["transaction_id"])
        ):
            raise ValueError("protocol must list exact requests")
        if derivation is None:
            if not isinstance(task["request_sha256"], str) or not task["request_sha256"]:
                raise ValueError("protocol must list exact requests")
        elif task["request_sha256"] is not None:
            raise ValueError("derived requests are reconstructed, never pre-listed")
    if len({t["task_id"] for t in protocol["tasks"]}) != len(protocol["tasks"]) or len(
        {t["transaction_id"] for t in protocol["tasks"]}
    ) != len(protocol["tasks"]):
        raise ValueError("protocol cases must be unique")
    money(protocol["usd_cap"])
    if "attempt_maximum_usd" in protocol:
        money(protocol["attempt_maximum_usd"])
    return protocol


def attempt_maximum(protocol: dict, computed: Decimal) -> Decimal:
    """The approved per-attempt maximum pinned in the envelope, else the computed one."""
    if "attempt_maximum_usd" in protocol:
        return money(protocol["attempt_maximum_usd"])
    return computed


def validate_approval_record(approval: dict) -> dict:
    """External owner approval of one exact protocol SHA. Returns a detached copy."""
    if not isinstance(approval, dict) or set(approval) != APPROVAL_FIELDS:
        raise ValueError("an exact external approval record is required")
    if approval["schema_version"] != APPROVAL_SCHEMA:
        raise ValueError("unsupported approval record version")
    if not _text(approval["approval_id"]) or not _text(approval["approver"]):
        raise ValueError("approval identity and approver are required")
    if not isinstance(approval["protocol_sha256"], str) or not HEX.match(
        approval["protocol_sha256"]
    ):
        raise ValueError("approval must bind one exact protocol SHA-256")
    if not _text(approval["owner_statement"], 4000):
        raise ValueError("approval must retain the owner's recorded statement")
    try:
        moment = datetime.fromisoformat(approval["approved_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("approval time must be an ISO timestamp") from exc
    if moment.tzinfo is None:
        raise ValueError("approval time requires a UTC offset")
    scope = approval["scope"]
    if not isinstance(scope, dict) or set(scope) != SCOPE_FIELDS:
        raise ValueError("approval scope must restate exact bounds")
    if PROVIDERS.get(scope["provider"]) != scope["model"]:
        raise ValueError("approval scope model is not pinned")
    if not _text(scope["purpose"]):
        raise ValueError("approval scope purpose is required")
    for key in ("case_count", "maximum_attempts"):
        if type(scope[key]) is not int or scope[key] <= 0:
            raise ValueError("approval scope counts must be positive integers")
    money(scope["usd_cap"])
    return json.loads(json.dumps(approval))


def _scope(protocols):
    cases = {(p["tenant_id"], p["run_id"], t["task_id"]) for p in protocols for t in p["tasks"]}
    return cases, sum((Decimal(p["usd_cap"]) for p in protocols), Decimal(0))


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
            return max(Decimal(0), PROVIDER_CAP - self._liability(cursor, provider))

    @staticmethod
    def _unresolved(cursor, provider):
        """Prior liabilities that must settle before another protocol may start."""
        rows = cursor.execute(
            "SELECT 'open protocol' AS kind, p.protocol_id AS identity "
            "FROM reckoner.v1_protocols p LEFT JOIN reckoner.v1_protocol_closures c "
            "USING (protocol_id) WHERE p.provider=%s AND c.protocol_id IS NULL "
            "UNION ALL SELECT 'unsettled call', c.call_id FROM reckoner.v1_provider_calls c "
            "WHERE c.provider=%s AND NOT EXISTS (SELECT 1 FROM reckoner.v1_settlements s "
            "WHERE s.call_id=c.call_id AND s.status='settled') ORDER BY 1,2",
            (provider, provider),
        ).fetchall()
        unresolved = [{"kind": r["kind"], "identity": r["identity"]} for r in rows]
        if provider == "anthropic":
            unresolved += [
                {"kind": "unsettled legacy call", "identity": r["call_id"]}
                for r in cursor.execute(
                    "SELECT call_id FROM reckoner.budget_entries WHERE status<>'settled' "
                    "ORDER BY call_id"
                ).fetchall()
            ]
        return unresolved

    def _ledger_id(self, cursor, provider):
        identity = cursor.execute(
            "SELECT ledger_uuid::text AS ledger_uuid FROM reckoner.v1_ledger_identity"
        ).fetchone()
        legacy = None
        if provider == "anthropic":
            rows = cursor.execute(
                "SELECT DISTINCT document->>'ledger_sha256' AS digest "
                "FROM reckoner.v1_legacy_provenance"
            ).fetchall()
            legacy = rows[0]["digest"] if len(rows) == 1 else None
        return (
            content_id(
                {
                    "provider": provider,
                    "ledger_uuid": identity["ledger_uuid"],
                    "legacy_ledger_sha256": legacy,
                }
            ),
            legacy,
        )

    def snapshot(self, provider: str) -> dict:
        """Reconciled provider-specific ledger state; a displayed credit is not consent."""
        if provider not in PROVIDERS:
            raise ValueError("unknown provider")
        with self._connection.transaction():
            cursor = self._cursor()
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
            ledger_id, legacy = self._ledger_id(cursor, provider)
            liability = self._liability(cursor, provider)
            legacy_verified = True
            if provider == "anthropic":
                try:
                    self._check_legacy(cursor)
                except BudgetExceeded:
                    legacy_verified = False
            return {
                "schema_version": "reckoner-ledger-snapshot-v1",
                "provider": provider,
                "ledger_id": ledger_id,
                "legacy_ledger_sha256": legacy,
                "legacy_verified": legacy_verified,
                "provider_cap_usd": format(PROVIDER_CAP, "f"),
                "liability_usd": format(liability, "f"),
                "remaining_usd": format(max(Decimal(0), PROVIDER_CAP - liability), "f"),
                "unresolved": self._unresolved(cursor, provider),
                "billing_basis": "local reservations and settlements; not a provider invoice",
            }

    def authorize(self, protocol_document: dict, dispatch: list[dict], *, approval: dict) -> str:
        """Owner action: record one external approval and reserve every full envelope.

        The approval binds the SHA-256 of the immutable protocol document. Duplicate
        approval IDs, a second approval of the same protocol, unresolved prior
        liabilities and provider-wide over-limit caps all block, atomically.
        """
        approval = validate_approval_record(approval)
        body = {k: v for k, v in protocol_document.items() if k != "protocol_sha256"}
        digest = protocol_document.get("protocol_sha256")
        if "approved" in protocol_document or digest != content_id(body):
            raise ValueError("protocol document identity mismatch")
        if approval["protocol_sha256"] != digest:
            raise ValueError("approval does not bind this protocol SHA-256")
        schema = protocol_document.get("schema_version")
        if schema == "reckoner-paid-protocol-v1":
            # The full body is validated, then bound (approver, purpose, exact cap, cases).
            from reckoner.v1.experiment.protocol import bind_approval, validate_body

            validate_body(protocol_document)
            bind_approval(approval, protocol_document)
        elif schema == FIXTURE_SCHEMA:
            # Explicitly labelled test fixtures only; production dispatch refuses them.
            if approval["approver"] != protocol_document.get("approver"):
                raise ValueError("approval is not from the protocol document's approver")
        else:
            raise ValueError(
                "only paid protocols or explicitly fixture-labelled test documents are authorized"
            )
        if not dispatch:
            raise ValueError("approval must cover at least one dispatch protocol")
        if protocol_document.get("dispatch") != dispatch:
            raise ValueError("recorded envelopes must be exactly the protocol's dispatch")
        scope = approval["scope"]
        for protocol in dispatch:
            validate_protocol(protocol)
            if (
                protocol["provider"] != scope["provider"]
                or protocol["model"] != scope["model"]
                or protocol["maximum_attempts"] > scope["maximum_attempts"]
            ):
                raise ValueError("dispatch protocol exceeds the approved scope")
        if len({p["protocol_id"] for p in dispatch}) != len(dispatch):
            raise ValueError("duplicate dispatch protocol")
        cases, total = _scope(dispatch)
        if len(cases) != scope["case_count"] or total != money(scope["usd_cap"]):
            raise ValueError("dispatch cases or caps differ from the approved scope")
        provider = scope["provider"]
        with self._connection.transaction():
            cursor = self._cursor()
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (ACCOUNTING_LOCK,))
            if cursor.execute(
                "SELECT 1 FROM reckoner.v1_protocol_approvals WHERE approval_id=%s",
                (approval["approval_id"],),
            ).fetchone():
                raise ValueError("duplicate approval identity")
            if cursor.execute(
                "SELECT 1 FROM reckoner.v1_protocol_approvals WHERE protocol_sha256=%s",
                (digest,),
            ).fetchone():
                raise ValueError("protocol is already approved")
            if provider == "anthropic":
                self._check_legacy(cursor)
            unresolved = self._unresolved(cursor, provider)
            if unresolved:
                raise BudgetExceeded(f"unresolved prior reservation: {unresolved[:5]}")
            if self._liability(cursor, provider) + total > PROVIDER_CAP:
                raise BudgetExceeded("provider-wide budget exceeded")
            cursor.execute(
                "INSERT INTO reckoner.v1_protocol_approvals (approval_id,protocol_sha256,document) "
                "VALUES (%s,%s,%s)",
                (approval["approval_id"], digest, Jsonb(approval)),
            )
            cursor.execute(
                "INSERT INTO reckoner.v1_experiment_protocols (protocol_sha256,document) "
                "VALUES (%s,%s)",
                (digest, Jsonb(protocol_document)),
            )
            for protocol in dispatch:
                if cursor.execute(
                    "SELECT 1 FROM reckoner.v1_protocols WHERE protocol_id=%s",
                    (protocol["protocol_id"],),
                ).fetchone():
                    raise ValueError("dispatch protocol was already reserved")
                cursor.execute(
                    "INSERT INTO reckoner.v1_protocols "
                    "(tenant_id,protocol_id,provider,run_id,maximum_cost,document) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        protocol["tenant_id"],
                        protocol["protocol_id"],
                        protocol["provider"],
                        protocol["run_id"],
                        Decimal(protocol["usd_cap"]),
                        Jsonb(protocol),
                    ),
                )
                cursor.execute(
                    "INSERT INTO reckoner.v1_protocol_authorizations "
                    "(tenant_id,protocol_id,protocol_sha256,approval_id) VALUES (%s,%s,%s,%s)",
                    (
                        protocol["tenant_id"],
                        protocol["protocol_id"],
                        digest,
                        approval["approval_id"],
                    ),
                )
        return digest

    def reserve(self, call: dict, maximum: Decimal, protocol: dict) -> dict:
        validate_protocol(protocol)
        BudgetLedger._validate_maximum(maximum)
        if "attempt_maximum_usd" in protocol and maximum != money(protocol["attempt_maximum_usd"]):
            raise ValueError("reservation differs from the approved per-attempt maximum")
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
        derived = protocol.get("derivation") is not None
        case = {
            "task_id": call["task_id"],
            "transaction_id": call["transaction_id"],
            "request_sha256": None if derived else call["request_sha256"],
        }
        if case not in protocol["tasks"]:
            raise ValueError("request is outside approved protocol")
        if derived and not (
            isinstance(call.get("derived_from"), dict) and call["derived_from"].get("call_id")
        ):
            raise ValueError("a derived request must record its persisted parent")
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
            if (
                derived
                and not cursor.execute(
                    "SELECT 1 FROM reckoner.v1_provider_calls c JOIN reckoner.v1_settlements s "
                    "ON s.call_id=c.call_id AND s.status='settled' JOIN "
                    "reckoner.v1_provider_responses r ON r.call_id=c.call_id "
                    "WHERE c.call_id=%s AND c.tenant_id=%s AND c.task_id=%s",
                    (call["derived_from"]["call_id"], call["tenant_id"], call["task_id"]),
                ).fetchone()
            ):
                raise ValueError("derived request parent is not a settled persisted response")
            current = cursor.execute(
                "SELECT p.document FROM reckoner.v1_protocols p "
                "JOIN reckoner.v1_protocol_authorizations a USING (tenant_id,protocol_id) "
                "WHERE p.protocol_id=%s",
                (protocol["protocol_id"],),
            ).fetchone()
            if not current:
                raise BudgetExceeded("protocol is not reserved under a recorded approval")
            if current["document"] != protocol:
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

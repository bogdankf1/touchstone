"""Stage validated OTLP documents in a disposable local warehouse snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

import duckdb

from touchstone_platform.contracts import ValidatedEvent
from touchstone_platform.extract import RejectedEvent, ValidatedDeclaration


@dataclass(frozen=True)
class StagingReceipt:
    cutoff: str
    accepted_measurements: int
    accepted_declarations: int
    rejected_count: int


def exact_cpst(
    model_cost: Decimal | None,
    review_cost: Decimal | None,
    error_cost: Decimal | None,
    correct_tasks: int | None,
    metrics_complete: bool,
) -> Decimal | None:
    """Divide warehouse components only; aggregation remains governed by dbt."""
    if (
        not metrics_complete
        or correct_tasks is None
        or correct_tasks <= 0
        or any(amount is None for amount in (model_cost, review_cost, error_cost))
    ):
        return None
    with localcontext() as context:
        context.prec = 50
        total = model_cost + review_cost + error_cost
        context.prec = 28
        return +(total / Decimal(correct_tasks))


def _money(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("invalid decimal money") from error
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -12:
        raise ValueError("money must be nonnegative DECIMAL(38,12)")
    if len(amount.as_tuple().digits) + max(amount.as_tuple().exponent, 0) > 38:
        raise ValueError("money exceeds DECIMAL(38,12)")
    if amount >= Decimal(10) ** 26:
        raise ValueError("money exceeds DECIMAL(38,12)")
    return amount


def build_snapshot(
    events: list[ValidatedEvent | RejectedEvent] | object,
    declarations: list[ValidatedDeclaration | RejectedEvent] | object,
    target: Path,
    *,
    through: datetime | None = None,
) -> StagingReceipt:
    """Build one closed DuckDB staging file from fixed-cutoff extraction iterators."""
    cutoff = (through or datetime.now(UTC)).astimezone(UTC).isoformat()
    target.parent.mkdir(parents=True, exist_ok=True)
    accepted_events = accepted_declarations = rejected = 0
    with duckdb.connect(str(target)) as connection:
        connection.execute("""
            create table raw_measurements (
                tenant_id varchar not null, workflow_id varchar not null,
                run_id varchar not null, task_id varchar not null,
                event_id varchar not null, event_kind varchar not null,
                node_name varchar not null, trace_id varchar not null,
                span_id varchar not null, received_at timestamptz not null,
                content_sha256 varchar not null, document_json json not null,
                cost_amount decimal(38,12), review_cost decimal(38,12),
                error_cost decimal(38,12)
            )
        """)
        connection.execute("""
            create table raw_declarations (
                tenant_id varchar not null, workflow_id varchar not null,
                run_id varchar not null, event_id varchar not null,
                received_at timestamptz not null, content_sha256 varchar not null,
                document_json json not null
            )
        """)
        connection.execute("""
            create table raw_rejections (
                tenant_id varchar, workflow_id varchar, run_id varchar,
                task_id varchar, received_at timestamptz not null,
                trace_id varchar, span_id varchar, event_name varchar,
                reason varchar not null
            )
        """)
        connection.execute("create table staging_metadata (cutoff timestamptz not null)")
        connection.execute("insert into staging_metadata values (?)", [cutoff])

        def reject(item: RejectedEvent, trusted: ValidatedEvent | None = None) -> None:
            nonlocal rejected
            connection.execute(
                "insert into raw_rejections values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    item.tenant_id,
                    trusted.workflow_id if trusted else None,
                    trusted.run_id if trusted else None,
                    trusted.task_id if trusted else None,
                    item.received_at,
                    item.trace_id,
                    item.span_id,
                    item.event_name,
                    item.reason,
                ],
            )
            rejected += 1

        for item in events:
            if isinstance(item, RejectedEvent):
                reject(item)
                continue
            document = item.document
            payload = document["payload"]
            try:
                cost = None
                review = None
                error = None
                if document["event_kind"] == "provider_usage":
                    cost = _money(payload.get("cost_amount"))
                if document["event_kind"] == "outcome":
                    review = _money(payload.get("review_cost"))
                    error = _money(payload.get("error_cost"))
            except ValueError as exc:
                reject(
                    RejectedEvent(
                        item.received_at,
                        item.trace_id,
                        item.span_id,
                        document["event_kind"],
                        str(exc),
                        item.tenant_id,
                    ),
                    trusted=item,
                )
                continue
            connection.execute(
                "insert into raw_measurements values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    item.tenant_id,
                    item.workflow_id,
                    item.run_id,
                    item.task_id,
                    item.identity.event_id,
                    document["event_kind"],
                    document["node_name"],
                    item.trace_id,
                    item.span_id,
                    item.received_at,
                    item.content_sha256,
                    item._document_json,
                    cost,
                    review,
                    error,
                ],
            )
            accepted_events += 1
        for item in declarations:
            if isinstance(item, RejectedEvent):
                reject(item)
                continue
            connection.execute(
                "insert into raw_declarations values (?, ?, ?, ?, ?, ?, ?)",
                [
                    item.identity.tenant_id,
                    item.identity.workflow_id,
                    item.identity.run_id,
                    item.identity.event_id,
                    item.received_at,
                    item.content_sha256,
                    item._document_json,
                ],
            )
            accepted_declarations += 1
        connection.checkpoint()
    return StagingReceipt(cutoff, accepted_events, accepted_declarations, rejected)

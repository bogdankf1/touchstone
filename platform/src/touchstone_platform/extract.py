"""Bounded extraction from the pinned Collector ClickHouse trace schema."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jsonschema import ValidationError

from touchstone_platform.contracts import (
    EventIdentity,
    ValidatedEvent,
    canonical_sha256,
    validate_declaration,
    validate_event,
    validate_event_context,
)

PAGE_SIZE = 500
_QUERY = """
SELECT ReceivedAt, ReceiptId, TraceId, SpanId, SpanAttributes,
       `Events.Name`, `Events.Attributes`
FROM otel.otel_traces
WHERE ReceivedAt <= {through:DateTime64(6)}
  AND (ReceivedAt, ReceiptId) > ({after_time:DateTime64(6)}, {after_id:UUID})
ORDER BY ReceivedAt, ReceiptId
LIMIT {page_size:UInt32}
"""


@dataclass(frozen=True)
class RejectedEvent:
    received_at: str
    trace_id: str
    span_id: str
    event_name: str
    reason: str
    tenant_id: str | None = None


@dataclass(frozen=True)
class ValidatedDeclaration:
    identity: EventIdentity
    trace_id: str
    span_id: str
    received_at: str
    content_sha256: str
    _document_json: str

    @property
    def document(self) -> dict[str, Any]:
        return json.loads(self._document_json)

    @property
    def tenant_id(self) -> str:
        return self.identity.tenant_id


def _rows(client: Any, through: datetime, page_size: int) -> Iterator[tuple]:
    if through.tzinfo is None or through.utcoffset() is None:
        raise ValueError("through must be timezone-aware")
    if page_size < 1 or page_size > 10_000:
        raise ValueError("page_size must be between 1 and 10000")
    after_time = datetime(1970, 1, 1, tzinfo=UTC)
    after_id = "00000000-0000-0000-0000-000000000000"
    while True:
        page = client.query(
            _QUERY,
            parameters={
                "through": through,
                "after_time": after_time,
                "after_id": after_id,
                "page_size": page_size,
            },
        ).result_rows
        if not page:
            return
        yield from page
        after_time, after_id = page[-1][:2]
        after_id = str(after_id)
        if len(page) < page_size:
            return


def _receipt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _context_value(value: Any) -> Any:
    if value == "true":
        return True
    if value == "false":
        return False
    return value


def _extract(client: Any, *, through: datetime, page_size: int, declaration: bool):
    for received, _, trace_id, span_id, attributes, names, event_attributes in _rows(
        client, through, page_size
    ):
        received_at = _receipt(received)
        context = {key: _context_value(value) for key, value in attributes.items()}
        for name, values in zip(names, event_attributes, strict=True):
            if declaration:
                if name != "touchstone.run.declaration":
                    continue
                key = "touchstone.run.json"
            else:
                if not name.startswith("touchstone.measurement."):
                    continue
                key = "touchstone.measurement.json"
            rejected_tenant = None
            try:
                raw = values.get(key)
                if not raw:
                    raise ValueError(f"missing {key}")
                document = json.loads(raw)
                if not isinstance(document, dict):
                    raise ValueError("envelope must be a JSON object")
                if declaration:
                    canonical = validate_declaration(document)
                    if context.get("touchstone.tenant_id") == canonical["tenant_id"]:
                        rejected_tenant = canonical["tenant_id"]
                    for field in ("tenant_id", "workflow_id", "workflow_version", "run_id"):
                        if context.get(f"touchstone.{field}") != canonical[field]:
                            raise ValueError(f"declaration {field} disagrees with enclosing span")
                    identity = EventIdentity(
                        canonical["tenant_id"],
                        canonical["workflow_id"],
                        canonical["run_id"],
                        canonical["event_id"],
                    )
                    yield ValidatedDeclaration(
                        identity,
                        trace_id,
                        span_id,
                        received_at,
                        canonical_sha256(canonical),
                        json.dumps(
                            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                        ),
                    )
                else:
                    event = validate_event(document, received_at)
                    if context.get("touchstone.tenant_id") == event.tenant_id:
                        rejected_tenant = event.tenant_id
                    validate_event_context(event, context, trace_id, span_id)
                    if name != f"touchstone.measurement.{document['event_kind']}":
                        raise ValueError("measurement event_kind disagrees with span event name")
                    yield event
            except (ValueError, TypeError, KeyError, ValidationError) as error:
                yield RejectedEvent(
                    received_at, trace_id, span_id, name, str(error), rejected_tenant
                )


def iter_measurements(
    client: Any, *, through: datetime, page_size: int = PAGE_SIZE
) -> Iterator[ValidatedEvent | RejectedEvent]:
    """Rebuild all measurements received through a fixed cutoff, including late events."""
    yield from _extract(client, through=through, page_size=page_size, declaration=False)


def iter_declarations(
    client: Any, *, through: datetime, page_size: int = PAGE_SIZE
) -> Iterator[ValidatedDeclaration | RejectedEvent]:
    """Rebuild declarations through the same receipt cutoff as measurements."""
    yield from _extract(client, through=through, page_size=page_size, declaration=True)

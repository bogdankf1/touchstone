"""Strict historical clocks and source-backed canonical retrieval."""

import csv
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckoner.contracts import content_id
from reckoner.data.adapter import adapt_row
from reckoner.v1.contracts import validate_v1

POLICY = "simulated-seven-days-v1"


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(UTC)


def eligible_before(occurred_at: str, available_at: str, query_at: str) -> bool:
    query = instant(query_at)
    return instant(occurred_at) < query and instant(available_at) < query


def historical_resolution(transaction: dict, label: str, policy_id: str) -> dict:
    if policy_id != POLICY:
        raise ValueError("unsupported historical resolution policy")
    if label not in {"fraud", "legitimate"}:
        raise ValueError("invalid historical label")
    body = {
        "schema_version": "reckoner-resolution-v1",
        "tenant_id": transaction["tenant_id"],
        "transaction_id": transaction["transaction_id"],
        "verdict": "decline" if label == "fraud" else "approve",
        "oracle_version": "cctd-label-v1",
        "resolved_at": (instant(transaction["occurred_at"]) + timedelta(days=7))
        .isoformat()
        .replace("+00:00", "Z"),
        "resolution_policy_version": policy_id,
        "provenance": {"dataset_simulated": True, "source": "simulated-label-oracle"},
    }
    return validate_v1("resolution", {**body, "resolution_id": content_id(body)})


class SourceHistory:
    """Preparation-role reader; runtime receives detached canonical rows only.

    The index keeps complete retained histories, including nonpositive amounts.
    Query cutoffs are strict, so tied timestamps never establish an order.
    Previous-card lookups deliberately ignore the display/evidence window.
    """

    def __init__(self, bundle: Path, source: Path):
        self.bundle = Path(bundle)
        self.source = Path(source)
        from reckoner.v1.data.prepare import verify_preparation

        self.index = verify_preparation(self.bundle, self.source)
        self.connection = sqlite3.connect(
            f"file:{self.bundle / 'history.sqlite'}?mode=ro", uri=True
        )

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def transaction(self, source_record: int) -> dict:
        item = self.connection.execute(
            "SELECT source_offset, tenant_id FROM history WHERE source_record=?", (source_record,)
        ).fetchone()
        if item is None:
            raise LookupError("historical source record unavailable")
        with (self.source / self.index["transaction_source"]).open("rb") as stream:
            headers = next(csv.reader([stream.readline().decode("utf-8-sig")]))
            stream.seek(item[0])
            values = next(csv.reader(line.decode("utf-8") for line in stream))
        adapted = adapt_row(
            dict(zip(headers, values, strict=True)),
            source_sha256=self.index["source_sha256"],
            source_record=source_record,
            tenant_id=item[1],
        )
        if adapted["transaction"] is None:
            raise ValueError("source-backed canonical row is invalid")
        return adapted["transaction"]

    def card_before(self, tenant_id: str, card_id: str, query_at: str, since: str | None = None):
        cutoff = instant(query_at).isoformat().replace("+00:00", "Z")
        lower = instant(since).isoformat().replace("+00:00", "Z") if since else None
        rows = self.connection.execute(
            "SELECT h.source_record FROM history h JOIN entities e ON e.entity_key=h.card_key "
            "WHERE h.tenant_id=? AND e.identity=? AND h.occurred_at<? "
            "AND (? IS NULL OR h.occurred_at>=?) ORDER BY h.occurred_at,h.source_record",
            (tenant_id, card_id, cutoff, lower, lower),
        )
        for row in rows:
            yield self.transaction(row[0])

    def previous_card(self, tenant_id: str, card_id: str, query_at: str):
        cutoff = instant(query_at).isoformat().replace("+00:00", "Z")
        row = self.connection.execute(
            "SELECT h.source_record FROM history h JOIN entities e ON e.entity_key=h.card_key "
            "WHERE h.tenant_id=? AND e.identity=? AND h.occurred_at<? "
            "ORDER BY h.occurred_at DESC,h.source_record LIMIT 1",
            (tenant_id, card_id, cutoff),
        ).fetchone()
        return self.transaction(row[0]) if row else None

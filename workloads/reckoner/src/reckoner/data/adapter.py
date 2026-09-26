"""Map simulated CCTD rows into the canonical runtime and oracle contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from reckoner.contracts import content_id
from reckoner.data.profile import amount_minor

REQUIRED_COLUMNS = {
    "User",
    "Card",
    "Year",
    "Month",
    "Day",
    "Time",
    "Amount",
    "Use Chip",
    "Merchant Name",
    "Merchant City",
    "Merchant State",
    "Zip",
    "MCC",
    "Errors?",
    "Is Fraud?",
}

_LABELS = {"No": "legitimate", "Yes": "fraud"}
_CHANNELS = {
    "Chip Transaction": "chip",
    "Swipe Transaction": "swipe",
    "Online Transaction": "online",
}


def transaction_id(source_sha256: str, source_record: int) -> str:
    """Return the stable identity of one CSV data record."""
    return content_id(
        {
            "dataset": "cctd",
            "source_sha256": source_sha256,
            "source_record": source_record,
        }
    )


def _optional(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _oracle(
    *,
    source_sha256: str,
    source_record: int,
    tenant_id: str,
    identity: str,
    label: str | None,
) -> dict[str, Any] | None:
    if label is None:
        return None
    return {
        "schema_version": "oracle-v1",
        "tenant_id": tenant_id,
        "transaction_id": identity,
        "label": label,
        "oracle_version": "cctd-label-v1",
        "source_file_sha256": source_sha256,
        "source_record": source_record,
    }


def adapt_row(
    row: dict[str, str],
    *,
    source_sha256: str,
    source_record: int,
    tenant_id: str,
) -> dict[str, Any]:
    """Adapt one source row while keeping the oracle outside runtime data."""
    if len(source_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_sha256
    ):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
    if not isinstance(source_record, int) or source_record < 1:
        raise ValueError("source_record must be a positive data-record ordinal")
    if not tenant_id:
        raise ValueError("tenant_id must not be empty")

    identity = transaction_id(source_sha256, source_record)
    label = _LABELS.get((row.get("Is Fraud?") or "").strip())
    oracle = _oracle(
        source_sha256=source_sha256,
        source_record=source_record,
        tenant_id=tenant_id,
        identity=identity,
        label=label,
    )

    missing = sorted(REQUIRED_COLUMNS - row.keys())
    if missing:
        return {
            "transaction": None,
            "oracle": oracle,
            "status": "invalid",
            "reason": f"missing required columns: {', '.join(missing)}",
        }
    if label is None:
        return {
            "transaction": None,
            "oracle": None,
            "status": "invalid",
            "reason": "invalid oracle label",
        }

    user = (row["User"] or "").strip()
    card = (row["Card"] or "").strip()
    merchant = (row["Merchant Name"] or "").strip()
    invalid_identifiers = [
        name
        for name, value in (("User", user), ("Card", card), ("Merchant Name", merchant))
        if not value
    ]
    if invalid_identifiers:
        return {
            "transaction": None,
            "oracle": oracle,
            "status": "invalid",
            "reason": f"invalid identifiers: {', '.join(invalid_identifiers)}",
        }

    try:
        occurred_at = datetime.strptime(
            f"{row['Year']}-{row['Month']}-{row['Day']} {row['Time']}",
            "%Y-%m-%d %H:%M",
        ).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return {
            "transaction": None,
            "oracle": oracle,
            "status": "invalid",
            "reason": "invalid transaction timestamp",
        }

    try:
        minor = amount_minor(row["Amount"])
    except (TypeError, ValueError):
        return {
            "transaction": None,
            "oracle": oracle,
            "status": "invalid",
            "reason": "invalid transaction amount",
        }

    raw_channel = (row["Use Chip"] or "").strip()
    channel = _CHANNELS.get(raw_channel, "other") if raw_channel else "unknown"
    raw_errors = (row["Errors?"] or "").strip()
    errors = [part.strip() for part in raw_errors.split(",") if part.strip()]
    transaction = {
        "schema_version": "transaction-v1",
        "event_id": content_id(
            {"event": "transaction-v1", "tenant_id": tenant_id, "transaction_id": identity}
        ),
        "tenant_id": tenant_id,
        "transaction_id": identity,
        "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
        "account_id": content_id(
            {
                "dataset": "cctd",
                "entity": "account",
                "tenant_id": tenant_id,
                "source_user": user,
            }
        ),
        "card_id": content_id(
            {
                "dataset": "cctd",
                "entity": "card",
                "tenant_id": tenant_id,
                "source_user": user,
                "source_card": card,
            }
        ),
        "merchant_id": content_id(
            {
                "dataset": "cctd",
                "entity": "merchant",
                "tenant_id": tenant_id,
                "source_merchant": merchant,
            }
        ),
        "amount_minor": minor,
        "currency": "USD",
        "payment_channel": channel,
        "merchant_category_code": _optional(row["MCC"]),
        "merchant_location": {
            "city": _optional(row["Merchant City"]),
            "region": _optional(row["Merchant State"]),
            "postal_code": _optional(row["Zip"]),
            "country": None,
        },
        "processing_errors": errors,
        "device_id": None,
        "ip_address": None,
        "provenance": {
            "dataset": "IBM Credit Card Transactions Dataset (CCTD)",
            "dataset_version": f"sha256:{source_sha256}",
            "source_file_sha256": source_sha256,
            "source_record": source_record,
            "adapter_version": "cctd-adapter-v1",
            "simulated": True,
            "normalization_assumptions": [
                "currency_assumed=USD",
                "source_timezone_assumed=UTC",
            ],
        },
    }
    return {
        "transaction": transaction,
        "oracle": oracle,
        "status": "eligible" if minor > 0 else "unsupported",
        "reason": None if minor > 0 else "nonpositive_amount",
    }

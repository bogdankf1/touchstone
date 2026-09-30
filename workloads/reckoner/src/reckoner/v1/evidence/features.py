"""Frozen structured similarity; no labels, tenant IDs or model embeddings."""

import math
from datetime import timedelta

from reckoner.contracts import content_id
from reckoner.v1.data.history import instant

NUMERIC = ("amount_usd", "prior_24h_count", "prior_30d_mean_usd", "seconds_since_previous")
CHANNELS = ("chip", "swipe", "online", "other", "unknown")


def history_features(transaction: dict, history: dict) -> dict:
    if history["status"] != "available":
        raise ValueError("history unavailable")
    if transaction["amount_minor"] <= 0:
        raise ValueError("nonpositive (zero or negative) USD amount unsupported for log1p features")
    query = instant(transaction["occurred_at"])
    rows = [
        r
        for r in history["transactions"]
        if r["tenant_id"] == transaction["tenant_id"]
        and r["card_id"] == transaction["card_id"]
        and query - timedelta(days=30) <= instant(r["occurred_at"]) < query
    ]
    purchases = [r["amount_minor"] / 100 for r in rows if r["amount_minor"] > 0]
    previous = history.get("previous")
    if previous is not None and (
        previous["tenant_id"] != transaction["tenant_id"]
        or previous["card_id"] != transaction["card_id"]
        or instant(previous["occurred_at"]) >= query
    ):
        raise ValueError("invalid previous-card cutoff")
    return {
        "amount_usd": transaction["amount_minor"] / 100,
        "prior_24h_count": sum(
            instant(r["occurred_at"]) >= query - timedelta(days=1) for r in rows
        ),
        "prior_30d_count": len(purchases),
        "prior_30d_mean_usd": sum(purchases) / len(purchases) if purchases else None,
        "seconds_since_previous": (query - instant(previous["occurred_at"])).total_seconds()
        if previous
        else None,
        "evidence_refs": sorted(r["transaction_id"] for r in rows),
        "status": "available",
    }


def _quantile(values, probability):
    values = sorted(values)
    target = sum(weight for _, weight in values) * probability
    cumulative = 0
    for value, weight in values:
        cumulative += weight
        if cumulative >= target:
            return value
    return values[-1][0]


def fit_scaler(development: list[dict]) -> dict:
    if not development or any(row.get("purpose") != "development" for row in development):
        raise ValueError("scaler requires development observations only")
    columns = [[] for _ in NUMERIC]
    for row in development:
        weight = float(row["weight"])
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("invalid inclusion weight")
        if not 2017 == instant(row["transaction"]["occurred_at"]).year:
            raise ValueError("scaler requires 2017 development observations")
        if row["history"]["status"] == "unavailable":
            amount = row["transaction"]["amount_minor"]
            if amount <= 0:
                raise ValueError("nonpositive USD amount unsupported for log1p features")
            values = dict.fromkeys(NUMERIC)
            values["amount_usd"] = amount / 100
        else:
            values = history_features(row["transaction"], row["history"])
        for column, name in zip(columns, NUMERIC, strict=True):
            if values[name] is not None:
                column.append((math.log1p(values[name]), weight))
    body = {
        "feature_version": "features-v1",
        "fit_purpose": "development",
        "quantile_method": "weighted-inverse-cdf-v1",
        "development_id": content_id(development),
        "median": [_quantile(c, 0.5) if c else 0 for c in columns],
        "iqr": [(_quantile(c, 0.75) - _quantile(c, 0.25)) or 1 if c else 1 for c in columns],
    }
    return {**body, "scaler_id": content_id(body)}


def feature_vector(transaction: dict, history: dict, scaler: dict) -> list[float]:
    if scaler["scaler_id"] != content_id({k: v for k, v in scaler.items() if k != "scaler_id"}):
        raise ValueError("scaler identity mismatch")
    values = history_features(transaction, history)
    numbers = [
        0 if values[name] is None else (math.log1p(values[name]) - median) / iqr
        for name, median, iqr in zip(NUMERIC, scaler["median"], scaler["iqr"], strict=True)
    ]
    channel = transaction.get("payment_channel", "unknown")
    if channel not in CHANNELS:
        channel = "unknown"
    return (
        numbers
        + [float(channel == name) for name in CHANNELS]
        + [
            float(values["prior_30d_mean_usd"] is None),
            float(values["seconds_since_previous"] is None),
        ]
    )

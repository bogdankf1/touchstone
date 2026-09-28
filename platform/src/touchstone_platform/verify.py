"""Acceptance-only comparison of a published run with an independent receipt."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from touchstone_platform.query import open_snapshot
from touchstone_platform.settings import Settings

_COUNTS = ("expected", "completed", "correct")
_MONEY = ("model_cost", "review_cost", "error_cost", "cpst")
_RATES = ("false_positive", "missed_fraud", "escalation")


def _compare_row(actual: dict, expected: dict, label: str) -> list[str]:
    mismatches = []
    for field in _COUNTS:
        if actual.get(f"{field}_tasks") != expected[field]:
            mismatches.append(f"{label}.{field}_tasks")
    for field in _MONEY:
        try:
            equal = Decimal(str(actual.get(field))) == Decimal(expected[field])
        except (InvalidOperation, TypeError):
            equal = False
        if not equal:
            mismatches.append(f"{label}.{field}")
    try:
        total = sum(Decimal(str(actual[field])) for field in _MONEY[:3])
        if total != Decimal(expected["total_cost"]):
            mismatches.append(f"{label}.total_cost")
    except (InvalidOperation, TypeError, KeyError):
        mismatches.append(f"{label}.total_cost")
    try:
        p99_ok = abs(
            Decimal(str(actual.get("latency_p99_ms"))) - Decimal(expected["p99_ms"])
        ) <= Decimal("0.000001")
    except (InvalidOperation, TypeError):
        p99_ok = False
    if not p99_ok:
        mismatches.append(f"{label}.p99_ms")
    if actual.get("metrics_complete") is not True:
        mismatches.append(f"{label}.metrics_complete")
    if actual.get("measurement_mode") != "measured":
        mismatches.append(f"{label}.measurement_mode")
    if actual.get("dataset_simulated") is not True:
        mismatches.append(f"{label}.dataset_simulated")
    rates = {item["metric_id"]: item for item in actual.get("contribution_rates", [])}
    for metric in _RATES:
        item = rates.get(metric, {})
        numerator, denominator = expected[metric]
        if item.get("numerator") != numerator or item.get("denominator") != denominator:
            mismatches.append(f"{label}.{metric}")
        else:
            try:
                observed_rate = Decimal(str(item.get("rate")))
                expected_rate = Decimal(numerator) / Decimal(denominator)
                rate_ok = abs(observed_rate - expected_rate) <= Decimal("0.000000000001")
            except (InvalidOperation, TypeError, ZeroDivisionError):
                rate_ok = False
            if not rate_ok:
                mismatches.append(f"{label}.{metric}.rate")
    return mismatches


def compare_summary(actual: dict, expected: dict) -> list[str]:
    """Return field paths that disagree; expected values never enter staging."""
    mismatches = []
    if actual.get("run_id") != expected.get("run_id"):
        mismatches.append("run_id")
    if actual.get("excluded_tenants"):
        mismatches.append("excluded_tenants")
    tenants = {item["tenant_id"]: item for item in actual.get("tenants", [])}
    if set(tenants) != {"tenant-a", "tenant-b"} or set(actual.get("tenant_ids", [])) != set(
        tenants
    ):
        mismatches.append("tenant_ids")
    mismatches.extend(_compare_row(actual, expected["aggregate"], "aggregate"))
    for tenant_id in ("tenant-a", "tenant-b"):
        if tenant_id not in tenants:
            mismatches.append(tenant_id)
            continue
        # Tenant rates are queried within the same pinned generation.
        mismatches.extend(_compare_row(tenants[tenant_id], expected[tenant_id], tenant_id))
    return mismatches


def verify_published(expected: dict, run_id: str, settings: Settings) -> tuple[str, list[str]]:
    with open_snapshot(settings) as snapshot:
        actual = snapshot.summary("reckoner", run_id, aggregate=True)
        if actual is None:
            return snapshot.metadata()["generation"], ["run not found"]
        for tenant in actual["tenants"]:
            tenant["contribution_rates"] = snapshot.contribution_rates(
                "reckoner", run_id, [tenant["tenant_id"]]
            )
        return snapshot.metadata()["generation"], compare_summary(actual, expected)

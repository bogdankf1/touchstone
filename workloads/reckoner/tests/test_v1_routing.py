"""Exact approved USD routing, independent of any provider."""

import importlib
from decimal import Decimal

import pytest


def router():
    try:
        return importlib.import_module("reckoner.v1.workflow.nodes").route
    except ModuleNotFoundError:
        pytest.fail("durable workflow routing is not implemented")


def thresholds(**changes):
    return {
        "currency": "USD",
        "review_cost": "4.00",
        "margin_rate": "0.30",
        "t_low_floor": "0.005",
        "t_low_ceiling": "0.05",
        "t_high": "0.90",
        "amount_aware": True,
        **changes,
    }


@pytest.mark.parametrize(
    "probability,amount,low,outcome",
    [
        (".004", "4000", ".005", "auto-approve"),
        (".005", "4000", ".005", "escalate"),
        (".05", "20", ".05", "escalate"),
        (".90", "20", ".05", "escalate"),
        (".901", "20", ".05", "auto-decline"),
        (".009", "400", ".01", "auto-approve"),
    ],
)
def test_exact_boundaries(probability, amount, low, outcome):
    result = router()(Decimal(probability), Decimal(amount), thresholds())
    assert result["outcome"] == outcome
    assert Decimal(result["effective_low_threshold"]) == Decimal(low)
    assert Decimal(result["effective_high_threshold"]) == Decimal(".90")


def test_flat_low_is_independent_of_amount_and_ceiling():
    result = router()(
        Decimal(".04"), Decimal("4000"), thresholds(amount_aware=False, t_low_ceiling=".02")
    )
    assert result["outcome"] == "auto-approve"
    assert Decimal(result["effective_low_threshold"]) == Decimal(".05")


def test_missing_probability_escalates():
    assert router()(None, Decimal("20"), thresholds())["outcome"] == "escalate"


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity"])
def test_invalid_amount_rejected(amount):
    with pytest.raises(ValueError, match="amount"):
        router()(Decimal(".1"), Decimal(amount), thresholds())


@pytest.mark.parametrize(
    "changes",
    [
        {"t_low_floor": "-.01"},
        {"t_low_floor": ".06"},
        {"t_high": ".05"},
        {"t_high": "1.01"},
        {"t_high": "NaN"},
        {"review_cost": "-1"},
        {"currency": "EUR"},
        {"amount_aware": "yes"},
        {"amount_aware": False, "t_low_ceiling": ".01", "t_high": ".04"},
    ],
)
def test_invalid_thresholds_rejected(changes):
    with pytest.raises(ValueError):
        router()(Decimal(".1"), Decimal("20"), thresholds(**changes))


@pytest.mark.parametrize("probability", ["-.01", "1.01", "NaN", "Infinity"])
def test_invalid_probability_rejected(probability):
    with pytest.raises(ValueError, match="probability"):
        router()(Decimal(probability), Decimal("20"), thresholds())

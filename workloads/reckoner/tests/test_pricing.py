import json
from decimal import Decimal

import pytest
from reckoner.baseline.pricing import (
    RESERVATION_FORMULA_VERSION,
    native_request_bytes,
    observed_cost,
    reservation_cost,
    reservation_input_bound,
)


def request(**changes):
    value = {
        "model": "claude-haiku-4-5-20251001",
        "system": "x",
        "messages": [{"role": "user", "content": "{}"}],
        "max_tokens": 256,
        "temperature": 0,
    }
    value.update(changes)
    return value


def prices(**changes):
    value = {"input_per_million": "1.00", "output_per_million": "5.00"}
    value.update(changes)
    return value


def result(**changes):
    value = {
        "requested_model": "anthropic/claude-haiku-4-5-20251001",
        "reported_model": "claude-haiku-4-5-20251001",
        "input_tokens": 31,
        "output_tokens": 7,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    value.update(changes)
    return value


def test_reservation_uses_conservative_input_and_max_output():
    assert reservation_cost(request(), 1000, prices()) == Decimal("0.004304")
    assert reservation_input_bound(request(), 1000) == 3024
    assert RESERVATION_FORMULA_VERSION == "anthropic-input-reservation-v1"


def test_reservation_counts_compact_native_utf8_bytes_not_escaped_characters():
    non_ascii = request(system="é" * 2600)
    native_document = {
        **non_ascii,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "{}"}]}],
    }
    expected_payload = json.dumps(
        native_document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    assert native_request_bytes(non_ascii) == expected_payload
    assert reservation_input_bound(non_ascii, 0) == len(expected_payload) + 1024


def test_reservation_rejects_an_input_bound_above_the_fixed_ceiling():
    with pytest.raises(ValueError, match="input reservation ceiling exceeded"):
        reservation_cost(request(system="x" * 8000), 0, prices())


@pytest.mark.parametrize("estimate", [-1, None, True])
def test_reservation_rejects_missing_or_negative_token_estimates(estimate):
    with pytest.raises(ValueError, match="estimated input tokens"):
        reservation_cost(request(), estimate, prices())


@pytest.mark.parametrize(
    "invalid_prices",
    [
        {"input_per_million": "1.00"},
        {"input_per_million": "unknown", "output_per_million": "5.00"},
        {"input_per_million": "-1.00", "output_per_million": "5.00"},
        {
            "model": "anthropic/another-model",
            "input_per_million": "1.00",
            "output_per_million": "5.00",
        },
    ],
)
def test_reservation_rejects_unknown_or_mismatched_prices(invalid_prices):
    with pytest.raises(ValueError, match="price"):
        reservation_cost(request(model="anthropic/claude-haiku-4-5-20251001"), 1, invalid_prices)


def test_observed_cost_preserves_exact_decimal_precision():
    exact_prices = {"input_per_million": "1.23456789", "output_per_million": "0"}

    assert observed_cost(result(input_tokens=1, output_tokens=0), exact_prices) == Decimal(
        "0.00000123456789"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"input_tokens": None},
        {"output_tokens": None},
        {"input_tokens": -1},
        {"output_tokens": -1},
        {"input_tokens": True},
        {"output_tokens": False},
        {"input_tokens": 31.9},
        {"output_tokens": 7.5},
        {"cache_read_tokens": None},
        {"cache_creation_tokens": None},
        {"cache_read_tokens": 1},
        {"cache_read_tokens": 0.5},
        {"cache_creation_tokens": False},
        {"cache_creation_tokens": 0.5},
        {"reported_model": "another-model"},
    ],
)
def test_observed_cost_is_unknown_for_missing_invalid_or_unpriced_usage(changes):
    assert observed_cost(result(**changes), prices()) is None


def test_malformed_but_billed_response_retains_usage_and_actual_overage():
    malformed = result(input_tokens=9000, output_tokens=256, content="not JSON")
    reserved = reservation_cost(request(), 1000, prices())
    actual = observed_cost(malformed, prices())

    assert actual == Decimal("0.010280")
    assert actual > reserved

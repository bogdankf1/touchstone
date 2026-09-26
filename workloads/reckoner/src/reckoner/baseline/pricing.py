"""Pinned cost calculations for Anthropic baseline calls."""

from __future__ import annotations

import json
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

RESERVATION_FORMULA_VERSION = "anthropic-input-reservation-v1"
INPUT_RESERVATION_CEILING = 8192
MAX_OUTPUT_TOKENS = 256
_MILLION = Decimal(1_000_000)


def native_model_id(model: str) -> str:
    """Remove only LiteLLM's explicit Anthropic routing prefix."""
    if not isinstance(model, str) or not model:
        raise ValueError("invalid model")
    prefix = "anthropic/"
    return model[len(prefix) :] if model.startswith(prefix) else model


def native_request_document(request: dict) -> dict[str, Any]:
    """Return the exact request document sent to Anthropic for generation."""
    try:
        messages = []
        for message in request["messages"]:
            if set(message) != {"role", "content"} or not isinstance(message["content"], str):
                raise ValueError("invalid request messages")
            messages.append(
                {
                    "role": message["role"],
                    "content": [{"type": "text", "text": message["content"]}],
                }
            )
        document = {
            "model": native_model_id(request["model"]),
            "system": request["system"],
            "messages": messages,
            "max_tokens": request["max_tokens"],
            "temperature": request["temperature"],
        }
    except KeyError as error:
        raise ValueError(f"missing request field: {error.args[0]}") from error
    return document


def native_request_bytes(request: dict) -> bytes:
    """Serialize the native request as compact canonical UTF-8 JSON."""
    try:
        return json.dumps(
            native_request_document(request),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("request is not valid JSON") from error


def reservation_input_bound(request: dict, estimated_input_tokens: int) -> int:
    """Return the pinned conservative input-token reservation bound."""
    if (
        isinstance(estimated_input_tokens, bool)
        or not isinstance(estimated_input_tokens, int)
        or estimated_input_tokens < 0
    ):
        raise ValueError("invalid estimated input tokens")
    bound = max(
        2 * estimated_input_tokens + 1024,
        len(native_request_bytes(request)) + 1024,
    )
    if bound > INPUT_RESERVATION_CEILING:
        raise ValueError("input reservation ceiling exceeded")
    return bound


def _price(prices: dict, field: str) -> Decimal:
    try:
        value = Decimal(prices[field])
    except (InvalidOperation, KeyError, TypeError) as error:
        raise ValueError(f"unknown {field} price") from error
    if not value.is_finite() or value < 0:
        raise ValueError(f"invalid {field} price")
    return value


def _validate_priced_model(requested_model: str, prices: dict) -> None:
    priced_model = prices.get("model")
    if priced_model is not None and priced_model != requested_model:
        raise ValueError("price model does not match request")


def reservation_cost(request: dict, estimated_input_tokens: int, prices: dict) -> Decimal:
    """Reserve the conservative maximum cost, rounded upward to one micro-dollar."""
    if request.get("max_tokens") != MAX_OUTPUT_TOKENS:
        raise ValueError("request output limit does not match reservation formula")
    requested_model = request.get("model")
    if not isinstance(requested_model, str):
        raise ValueError("invalid model")
    _validate_priced_model(requested_model, prices)
    bound = reservation_input_bound(request, estimated_input_tokens)
    reserved = (
        Decimal(bound) * _price(prices, "input_per_million")
        + Decimal(MAX_OUTPUT_TOKENS) * _price(prices, "output_per_million")
    ) / _MILLION
    return reserved.quantize(Decimal("0.000001"), rounding=ROUND_CEILING)


def _usage_count(result: dict, field: str) -> int | None:
    value = result.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def observed_cost(result: dict, prices: dict) -> Decimal | None:
    """Calculate exact token cost, or return unknown when usage cannot be priced."""
    requested_model = result.get("requested_model")
    reported_model = result.get("reported_model")
    if not isinstance(requested_model, str) or not isinstance(reported_model, str):
        return None
    if native_model_id(requested_model) != reported_model:
        return None
    try:
        _validate_priced_model(requested_model, prices)
        input_price = _price(prices, "input_per_million")
        output_price = _price(prices, "output_per_million")
    except ValueError:
        return None

    input_tokens = _usage_count(result, "input_tokens")
    output_tokens = _usage_count(result, "output_tokens")
    cache_read = _usage_count(result, "cache_read_tokens")
    cache_creation = _usage_count(result, "cache_creation_tokens")
    if None in (input_tokens, output_tokens, cache_read, cache_creation):
        return None
    if cache_read or cache_creation:
        return None
    return (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price) / _MILLION

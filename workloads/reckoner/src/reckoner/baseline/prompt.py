"""Build the fixed, oracle-free baseline prompt."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from reckoner.contracts import content_id

FACT_FIELDS = (
    "amount_minor",
    "currency",
    "occurred_at",
    "payment_channel",
    "merchant_category_code",
    "merchant_location",
    "processing_errors",
)
MAX_USER_CONTENT_BYTES = 4096
_TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "prompts" / "baseline-v1.txt"
_SYSTEM_TEMPLATE = _TEMPLATE_PATH.read_text(encoding="utf-8").rstrip()
_CONSTRUCTION_RULES = {
    "schema_version": "baseline-prompt-construction-v1",
    "fact_fields": FACT_FIELDS,
    "user_message_encoding": "canonical-json-utf8",
    "user_message_role": "user",
    "user_message_count": 1,
    "max_user_content_bytes": MAX_USER_CONTENT_BYTES,
}
PROMPT_VERSION = content_id(
    {"system_template": _SYSTEM_TEMPLATE, "construction_rules": _CONSTRUCTION_RULES}
)


def _decimal_parameter(thresholds: dict, name: str) -> Decimal:
    try:
        value = Decimal(thresholds["parameters"][name])
    except (InvalidOperation, KeyError, TypeError) as error:
        raise ValueError(f"invalid threshold parameter: {name}") from error
    if not value.is_finite() or value < 0:
        raise ValueError(f"invalid threshold parameter: {name}")
    return value


def _percentage(value: Decimal) -> str:
    rendered = format(value * 100, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def build_request(transaction: dict, config: dict, thresholds: dict) -> dict[str, Any]:
    """Return the only model-facing fields for one simulated purchase."""
    if config.get("prompt_version") != PROMPT_VERSION:
        raise ValueError("prompt version does not match construction")

    try:
        facts = {name: transaction[name] for name in FACT_FIELDS}
    except KeyError as error:
        raise ValueError(f"missing prompt fact: {error.args[0]}") from error

    content = json.dumps(
        facts,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(content.encode("utf-8")) > MAX_USER_CONTENT_BYTES:
        raise ValueError("request content exceeds prompt limit")

    review_cost = _decimal_parameter(thresholds, "review_cost")
    margin_rate = _decimal_parameter(thresholds, "margin_rate")
    if margin_rate > 1:
        raise ValueError("invalid threshold parameter: margin_rate")
    system = _SYSTEM_TEMPLATE.replace("${review_cost}", format(review_cost, ".2f")).replace(
        "${margin_rate_percent}", _percentage(margin_rate)
    )

    return {
        "model": config["model"],
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": config["max_output_tokens"],
        "temperature": config["temperature"],
    }

"""Load and validate immutable baseline run configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from reckoner.contracts import content_id, validate_document
from reckoner.resources import SCHEMAS

SUPPORTED_MODELS = {"anthropic": {"anthropic/claude-haiku-4-5-20251001"}}


class FrozenDict(dict):
    """A JSON-compatible mapping whose values cannot change after validation."""

    def _immutable(self, *args: object, **kwargs: object) -> None:
        raise TypeError("validated configuration is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _read(path: Path, description: str) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {description} document") from error
    if not isinstance(document, dict):
        raise ValueError(f"invalid {description} document")
    return document


def _validate_identity(document: dict[str, Any], identity: str, description: str) -> None:
    body = {key: value for key, value in document.items() if key != identity}
    if document.get(identity) != content_id(body):
        raise ValueError(f"{description} identity does not match contents")


def load_config(path: Path, price_path: Path) -> dict:
    """Return a validated, deeply immutable run configuration snapshot."""
    config = _read(path, "config")
    prices = _read(price_path, "price")
    try:
        validate_document(config, SCHEMAS / "run-config-v1.schema.json")
    except ValidationError as error:
        raise ValueError("invalid config document") from error
    try:
        validate_document(prices, SCHEMAS / "price-table-v1.schema.json")
    except ValidationError as error:
        raise ValueError("invalid price document") from error
    _validate_identity(config, "config_id", "config")
    _validate_identity(prices, "price_table_version", "price")

    provider = config["provider"]
    if provider not in SUPPORTED_MODELS:
        raise ValueError("unsupported provider")
    if config["model"] not in SUPPORTED_MODELS[provider]:
        raise ValueError("unsupported model")
    if prices["model"] != config["model"]:
        raise ValueError("model is not priced by selected price table")
    if prices["price_table_version"] != config["price_table_version"]:
        raise ValueError("price table identity does not match config")
    if prices["currency"] != "USD":
        raise ValueError("price currency must be USD")
    return _freeze(config)

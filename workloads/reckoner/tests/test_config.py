import copy
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError
from reckoner.baseline.config import load_config
from reckoner.contracts import content_id, validate_document

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "workloads/reckoner/config/baseline-v1.json"
NATIVE_CONFIG = ROOT / "workloads/reckoner/config/baseline-v2.json"
PRICES = ROOT / "workloads/reckoner/config/anthropic-prices-v1.json"
SCHEMAS = ROOT / "contracts/schemas"


def _documents(tmp_path):
    config = json.loads(CONFIG.read_text())
    prices = json.loads(PRICES.read_text())
    config_path = tmp_path / "config.json"
    price_path = tmp_path / "prices.json"
    config_path.write_text(json.dumps(config))
    price_path.write_text(json.dumps(prices))
    return config, prices, config_path, price_path


def _identify(document, field):
    document[field] = content_id({key: value for key, value in document.items() if key != field})


def test_load_config_returns_deeply_immutable_validated_content():
    config = load_config(CONFIG, PRICES)

    assert config["model"] == "anthropic/claude-haiku-4-5-20251001"
    assert config["temperature"] == 0
    assert config["max_output_tokens"] == 256
    assert config["timeout_seconds"] == 60
    assert config["input_token_ceiling"] == 8192
    assert set(config["threshold_config_ids"]) == {"tenant-a", "tenant-b"}
    with pytest.raises(TypeError):
        config["model"] = "changed"
    with pytest.raises(TypeError):
        config["threshold_config_ids"]["tenant-a"] = "0" * 64


@pytest.mark.parametrize(
    ("target", "mutation", "message"),
    [
        ("config", lambda d: d.update({"unexpected": True}), "config"),
        ("config", lambda d: d.update({"retries": 1}), "config"),
        ("config", lambda d: d.update({"model": "anthropic/unpriced"}), "model"),
        ("config", lambda d: d.update({"provider": "other"}), "provider"),
        ("prices", lambda d: d.update({"input_per_million": "NaN"}), "price"),
        ("prices", lambda d: d.update({"output_per_million": "-1"}), "price"),
    ],
)
def test_load_config_rejects_unknown_or_unsupported_settings(tmp_path, target, mutation, message):
    config, prices, config_path, price_path = _documents(tmp_path)
    document = config if target == "config" else prices
    mutation(document)
    _identify(document, "config_id" if target == "config" else "price_table_version")
    (config_path if target == "config" else price_path).write_text(json.dumps(document))

    with pytest.raises(ValueError, match=message):
        load_config(config_path, price_path)


@pytest.mark.parametrize(
    ("target", "field", "value"),
    [
        ("config", "prompt_version", "baseline-prompt-v2"),
        ("config", "price_table_version", "0" * 64),
        ("prices", "input_per_million", "2.00"),
    ],
)
def test_load_config_rejects_tampering_without_a_new_identity(tmp_path, target, field, value):
    config, prices, config_path, price_path = _documents(tmp_path)
    document = config if target == "config" else prices
    document[field] = value
    (config_path if target == "config" else price_path).write_text(json.dumps(document))

    with pytest.raises(ValueError, match="identity"):
        load_config(config_path, price_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", 100),
        ("max_output_tokens", 1_000_000),
        ("timeout_seconds", 1),
        ("input_token_ceiling", 1),
    ],
)
def test_load_config_rejects_reidentified_changes_to_fixed_execution_limits(tmp_path, field, value):
    config, _, config_path, price_path = _documents(tmp_path)
    config[field] = value
    _identify(config, "config_id")
    config_path.write_text(json.dumps(config))

    with pytest.raises(ValueError, match="config"):
        load_config(config_path, price_path)


def test_config_and_decision_documents_obey_strict_contracts():
    config = json.loads(CONFIG.read_text())
    prices = json.loads(PRICES.read_text())
    validate_document(config, SCHEMAS / "run-config-v1.schema.json")
    validate_document(prices, SCHEMAS / "price-table-v1.schema.json")
    validate_document({"outcome": "escalate"}, SCHEMAS / "decision-v1.schema.json")

    invalid = copy.deepcopy(config)
    invalid["threshold_config_ids"] = {}
    with pytest.raises(ValidationError):
        validate_document(invalid, SCHEMAS / "run-config-v1.schema.json")


def test_native_config_loads_with_immutable_schema_and_historical_config_still_loads():
    native = load_config(NATIVE_CONFIG, PRICES)
    historical = load_config(CONFIG, PRICES)
    assert native["config_id"] != historical["config_id"]
    assert native["output_config"]["format"]["schema"]["required"] == ("outcome",)
    with pytest.raises(TypeError):
        native["output_config"]["format"]["schema"]["additionalProperties"] = True


def test_native_config_rejects_reidentified_schema_change(tmp_path):
    native = json.loads(NATIVE_CONFIG.read_text(encoding="utf-8"))
    native["output_config"]["format"]["schema"]["additionalProperties"] = True
    _identify(native, "config_id")
    path = tmp_path / "baseline-v2.json"
    path.write_text(json.dumps(native))

    with pytest.raises(ValueError, match="config"):
        load_config(path, PRICES)

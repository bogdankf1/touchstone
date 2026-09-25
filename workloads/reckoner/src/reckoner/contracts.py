import hashlib
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


def content_id(document: dict) -> str:
    payload = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_document(document: dict, schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)


def validate_threshold_config(document: dict, schema_path: Path) -> None:
    validate_document(document, schema_path)
    body = {key: value for key, value in document.items() if key != "config_id"}
    if document["config_id"] != content_id(body):
        raise ValueError("config identity does not match contents")

    parameters = document["parameters"]
    if Decimal(parameters["review_cost"]) < 0:
        raise ValueError("negative review cost")
    if not Decimal(0) <= Decimal(parameters["margin_rate"]) <= Decimal(1):
        raise ValueError("margin rate is outside [0,1]")

    low = Decimal(parameters["t_low_floor"])
    ceiling = Decimal(parameters["t_low_ceiling"])
    high = Decimal(parameters["t_high"])
    if not Decimal(0) <= low <= ceiling < high <= Decimal(1):
        raise ValueError("invalid threshold ordering")
    if not parameters["amount_aware"] and high <= Decimal("0.05"):
        raise ValueError("flat threshold must be below high threshold")


def validate_cohort_manifest(document: dict, schema_path: Path) -> None:
    validate_document(document, schema_path)
    body = {key: value for key, value in document.items() if key != "manifest_id"}
    if document["manifest_id"] != content_id(body):
        raise ValueError("manifest identity does not match contents")

    history_end = datetime.fromisoformat(document["history_end"])
    start = datetime.fromisoformat(document["evaluation_start"])
    end = datetime.fromisoformat(document["evaluation_end"])
    if not history_end < start <= end:
        raise ValueError("invalid temporal boundary")

    counts = document["counts"]
    if counts["fraud"] + counts["legitimate"] != counts["total"]:
        raise ValueError("class counts do not sum to total")
    if len(document["selected_transaction_ids"]) != counts["total"]:
        raise ValueError("selected IDs do not match total")

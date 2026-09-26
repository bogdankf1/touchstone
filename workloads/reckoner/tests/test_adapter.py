import csv
import io
from pathlib import Path

import pytest
from jsonschema import ValidationError
from reckoner.contracts import validate_document
from reckoner.data.adapter import adapt_row

ROOT = Path(__file__).resolve().parents[3]
TRANSACTION_SCHEMA = ROOT / "contracts" / "schemas" / "transaction-v1.schema.json"
ORACLE_SCHEMA = ROOT / "contracts" / "schemas" / "oracle-v1.schema.json"


def source_row(amount="$20.01", label="No", **changes):
    row = {
        "User": "0",
        "Card": "0",
        "Year": "2019",
        "Month": "1",
        "Day": "2",
        "Time": "12:30",
        "Amount": amount,
        "Use Chip": "Swipe Transaction",
        "Merchant Name": "123",
        "Merchant City": "X",
        "Merchant State": "Y",
        "Zip": "00123",
        "MCC": "1234",
        "Errors?": "",
        "Is Fraud?": label,
    }
    row.update(changes)
    return row


def adapt(row):
    return adapt_row(
        row,
        source_sha256="a" * 64,
        source_record=1,
        tenant_id="tenant-a",
    )


def test_adapter_separates_oracle_and_keeps_duplicate_rows_distinct():
    first = adapt_row(source_row(), source_sha256="a" * 64, source_record=1, tenant_id="tenant-a")
    second = adapt_row(source_row(), source_sha256="a" * 64, source_record=2, tenant_id="tenant-a")

    assert first["status"] == "eligible"
    assert first["transaction"]["amount_minor"] == 2001
    assert first["transaction"]["occurred_at"] == "2019-01-02T12:30:00Z"
    assert first["transaction"]["currency"] == "USD"
    assert first["transaction"]["transaction_id"] != second["transaction"]["transaction_id"]
    assert "label" not in first["transaction"]
    assert first["oracle"]["label"] == "legitimate"
    assert first["transaction"]["provenance"]["normalization_assumptions"] == [
        "currency_assumed=USD",
        "source_timezone_assumed=UTC",
    ]


@pytest.mark.parametrize(
    ("amount", "status", "amount_minor"),
    [
        ("$0.00", "unsupported", 0),
        ("$-1.00", "unsupported", -100),
        ("$1,234.56", "eligible", 123456),
        ("$1.001", "invalid", None),
        ("NaN", "invalid", None),
        ("Infinity", "invalid", None),
    ],
)
def test_adapter_classifies_money_without_rounding(amount, status, amount_minor):
    result = adapt(source_row(amount=amount))

    assert result["status"] == status
    if amount_minor is None:
        assert result["transaction"] is None
    else:
        assert result["transaction"]["amount_minor"] == amount_minor
        assert result["oracle"]["label"] == "legitimate"


@pytest.mark.parametrize(
    ("changes", "oracle_present"),
    [
        ({"Day": "31", "Month": "2"}, True),
        ({"User": ""}, True),
        ({"Card": ""}, True),
        ({"Merchant Name": ""}, True),
        ({"Is Fraud?": "Maybe"}, False),
    ],
)
def test_adapter_rejects_invalid_dates_identifiers_and_labels(changes, oracle_present):
    result = adapt(source_row(**changes))

    assert result["status"] == "invalid"
    assert result["transaction"] is None
    assert (result["oracle"] is not None) is oracle_present
    assert result["reason"]


def test_adapter_rejects_missing_required_columns_without_disclosing_values():
    row = source_row()
    row.pop("Amount")
    row["extra"] = "do-not-disclose"

    result = adapt(row)

    assert result["status"] == "invalid"
    assert result["transaction"] is None
    assert "Amount" in result["reason"]
    assert "do-not-disclose" not in result["reason"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Chip Transaction", "chip"),
        ("Swipe Transaction", "swipe"),
        ("Online Transaction", "online"),
        ("Contactless", "other"),
        ("", "unknown"),
    ],
)
def test_adapter_normalizes_channels(raw, expected):
    result = adapt(source_row(**{"Use Chip": raw}))

    assert result["transaction"]["payment_channel"] == expected


def test_adapter_preserves_optional_nulls_postal_text_and_embedded_newline():
    result = adapt(
        source_row(
            **{
                "Merchant City": "North\nHarbor",
                "Merchant State": "",
                "Zip": "00123",
                "MCC": "",
                "Errors?": "",
            }
        )
    )

    transaction = result["transaction"]
    assert transaction["merchant_location"] == {
        "city": "North\nHarbor",
        "region": None,
        "postal_code": "00123",
        "country": None,
    }
    assert transaction["merchant_category_code"] is None
    assert transaction["processing_errors"] == []
    assert transaction["device_id"] is None
    assert transaction["ip_address"] is None


def test_quoted_csv_record_is_one_data_record_even_with_embedded_newline():
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(source_row()))
    writer.writeheader()
    writer.writerow(source_row(**{"Merchant City": "North\nHarbor"}))
    parsed = list(csv.DictReader(io.StringIO(stream.getvalue(), newline="")))

    result = adapt_row(parsed[0], source_sha256="a" * 64, source_record=1, tenant_id="tenant-a")

    assert len(parsed) == 1
    assert result["transaction"]["provenance"]["source_record"] == 1
    assert result["transaction"]["merchant_location"]["city"] == "North\nHarbor"


def test_adapter_outputs_validate_against_runtime_and_oracle_schemas():
    result = adapt(source_row(label="Yes"))

    validate_document(result["transaction"], TRANSACTION_SCHEMA)
    validate_document(result["oracle"], ORACLE_SCHEMA)


def test_runtime_schema_rejects_oracle_leakage():
    transaction = adapt(source_row())["transaction"]
    transaction["label"] = "legitimate"

    with pytest.raises(ValidationError):
        validate_document(transaction, TRANSACTION_SCHEMA)

import csv
import hashlib
import json

import pytest
from reckoner.data.profile import inventory, main, profile_transactions

HEADERS = [
    "User",
    "Card",
    "Year",
    "Month",
    "Day",
    "Time",
    "Amount",
    "Use Chip",
    "Merchant Name",
    "Merchant City",
    "Merchant State",
    "Zip",
    "MCC",
    "Errors?",
    "Is Fraud?",
]


def write_rows(path, rows, headers=HEADERS):
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)


def row(amount="$20.01", label="No", day="1", card="0"):
    return [
        "0",
        card,
        "2020",
        "1",
        day,
        "12:30",
        amount,
        "Swipe Transaction",
        "merchant-1",
        "X",
        "Y",
        "",
        "1234",
        "",
        label,
    ]


def test_retains_duplicate_looking_records_and_counts_fraud(tmp_path):
    path = tmp_path / "transactions.csv"
    write_rows(path, [row(), row(), row(label="Yes")])

    result = profile_transactions(path, {("0", "0")})

    assert result["row_count"] == 3
    assert result["legitimate_count"] == 2
    assert result["fraud_count"] == 1
    assert result["positive_amount_total_minor"] == 6003
    assert result["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_reports_invalid_inputs_without_losing_rows(tmp_path):
    path = tmp_path / "transactions.csv"
    write_rows(
        path,
        [
            row(amount="$0.00"),
            row(amount="$-1.00"),
            row(amount="unknown"),
            row(label="Maybe"),
            row(day="32"),
            row(card="missing"),
        ],
    )

    result = profile_transactions(path, {("0", "0")})

    assert result["row_count"] == 6
    assert result["nonpositive_amount_count"] == 2
    assert result["invalid_amount_count"] == 1
    assert result["unknown_label_count"] == 1
    assert result["invalid_timestamp_count"] == 1
    assert result["unmatched_card_count"] == 1


def test_parses_quoted_amount_containing_a_comma(tmp_path):
    path = tmp_path / "transactions.csv"
    write_rows(path, [row(amount="$1,234.56")])

    result = profile_transactions(path, {("0", "0")})

    assert result["positive_amount_total_minor"] == 123456
    assert result["invalid_amount_count"] == 0


def test_missing_required_headers_names_columns_without_source_values(tmp_path):
    path = tmp_path / "transactions.csv"
    secret = "source-value-must-not-appear"
    write_rows(path, [[secret]], headers=["User"])

    with pytest.raises(ValueError) as error:
        profile_transactions(path, set())

    assert "Amount" in str(error.value)
    assert "Is Fraud?" in str(error.value)
    assert secret not in str(error.value)


def test_empty_transaction_file_has_empty_aggregates(tmp_path):
    path = tmp_path / "transactions.csv"
    write_rows(path, [])

    result = profile_transactions(path, set())

    assert result["row_count"] == 0
    assert result["time_range"] == {"earliest": None, "latest": None}
    assert result["per_user"] == {}


def test_inventory_does_not_infer_user_identity_from_row_position(tmp_path):
    write_rows(tmp_path / "credit_card_transactions-ibm_v2.csv", [row()])
    write_rows(tmp_path / "User0_credit_card_transactions.csv", [row()])
    write_rows(
        tmp_path / "sd254_cards.csv",
        [["0", "0", "Visa"]],
        headers=["User", "CARD INDEX", "Card Brand"],
    )
    write_rows(
        tmp_path / "sd254_users.csv",
        [["Ada Lovelace", "36"]],
        headers=["Person", "Current Age"],
    )

    result = inventory(tmp_path)

    assert result["joins"]["transactions_to_cards"]["status"] == "verified"
    assert result["joins"]["users"]["status"] == "unverified"
    assert result["files"]["User0_credit_card_transactions.csv"]["included_in_main_counts"] is False
    assert result["provenance"]["simulated"] is True
    assert result["assumptions"]["timezone"] == "unverified"
    assert result["assumptions"]["currency"] == "unverified"


def test_inventory_reports_unmatched_card_references(tmp_path):
    write_rows(
        tmp_path / "credit_card_transactions-ibm_v2.csv",
        [row(), row(card="9")],
    )
    write_rows(tmp_path / "User0_credit_card_transactions.csv", [])
    write_rows(
        tmp_path / "sd254_cards.csv",
        [["0", "0"]],
        headers=["User", "CARD INDEX"],
    )
    write_rows(tmp_path / "sd254_users.csv", [], headers=["Person"])

    result = inventory(tmp_path)

    assert result["transactions"]["unmatched_card_count"] == 1
    assert result["joins"]["transactions_to_cards"]["status"] == "unverified"


def test_cli_writes_requested_aggregate_report(tmp_path, capsys):
    archive = tmp_path / "archive"
    archive.mkdir()
    write_rows(archive / "credit_card_transactions-ibm_v2.csv", [row(label="Yes")])
    write_rows(archive / "User0_credit_card_transactions.csv", [])
    write_rows(
        archive / "sd254_cards.csv",
        [["0", "0"]],
        headers=["User", "CARD INDEX"],
    )
    write_rows(archive / "sd254_users.csv", [], headers=["Person"])
    output = tmp_path / "nested" / "inventory.json"

    exit_code = main(["--archive", str(archive), "--output", str(output)])

    assert exit_code == 0
    assert json.loads(output.read_text())["transactions"]["fraud_count"] == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["row_count"] == 1
    assert "per_user" not in printed


def test_cli_returns_two_for_missing_source_files(tmp_path, capsys):
    archive = tmp_path / "archive"
    archive.mkdir()
    output = tmp_path / "inventory.json"

    exit_code = main(["--archive", str(archive), "--output", str(output)])

    assert exit_code == 2
    assert not output.exists()
    assert "missing source files" in capsys.readouterr().err


def test_cli_returns_two_for_undecodable_existing_input(tmp_path, capsys):
    archive = tmp_path / "archive"
    archive.mkdir()
    write_rows(archive / "credit_card_transactions-ibm_v2.csv", [row()])
    write_rows(archive / "User0_credit_card_transactions.csv", [])
    write_rows(
        archive / "sd254_cards.csv",
        [["0", "0"]],
        headers=["User", "CARD INDEX"],
    )
    (archive / "sd254_users.csv").write_bytes(b"\xff")
    output = tmp_path / "inventory.json"

    exit_code = main(["--archive", str(archive), "--output", str(output)])

    assert exit_code == 2
    assert not output.exists()
    assert "source inventory failed" in capsys.readouterr().err


def test_cli_refuses_to_write_inside_source_archive(tmp_path, capsys):
    output = tmp_path / "inventory.json"

    exit_code = main(["--archive", str(tmp_path), "--output", str(output)])

    assert exit_code == 2
    assert not output.exists()
    assert "outside the source archive" in capsys.readouterr().err

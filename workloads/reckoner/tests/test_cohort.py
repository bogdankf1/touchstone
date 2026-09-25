import csv
import json
from pathlib import Path

from reckoner.data.artifacts import load_runtime
from reckoner.data.cohort import prepare

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


def row(user, year, label, ordinal, amount="$20.01"):
    return {
        "User": user,
        "Card": "0",
        "Year": str(year),
        "Month": "1",
        "Day": str(ordinal % 28 + 1),
        "Time": f"{ordinal % 24:02d}:{ordinal % 60:02d}",
        "Amount": amount,
        "Use Chip": "Swipe Transaction",
        "Merchant Name": str(1000 + ordinal % 7),
        "Merchant City": "North\nHarbor" if ordinal == 0 else "X",
        "Merchant State": "Y",
        "Zip": "00123",
        "MCC": "1234",
        "Errors?": "",
        "Is Fraud?": label,
    }


def write_source(source_dir: Path, *, extra_holdout=False, flip_holdout=False):
    source_dir.mkdir()
    rows = []
    for index in range(18):
        rows.append(row("history-low", 2018, "No", index))
    for index in range(2):
        rows.append(row("history-high", 2018, "Yes", 100 + index))

    fraud_count = 101 if extra_holdout else 100
    legitimate_count = 901 if extra_holdout else 900
    for index in range(fraud_count):
        label = "No" if flip_holdout and index == 0 else "Yes"
        rows.append(row("late" if index == 0 else "history-high", 2019, label, 200 + index))
    for index in range(legitimate_count):
        rows.append(row("history-low", 2019, "No", 500 + index))
    rows.append(row("future-only", 2020, "No", 1700))
    rows.append(row("retained-fraud", 2017, "Yes", 1800, amount="$-1.00"))

    transaction_path = source_dir / "credit_card_transactions-ibm_v2.csv"
    with transaction_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)

    with (source_dir / "User0_credit_card_transactions.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=HEADERS)
        writer.writeheader()

    users = sorted({item["User"] for item in rows})
    with (source_dir / "sd254_cards.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["User", "CARD INDEX"])
        writer.writeheader()
        writer.writerows({"User": user, "CARD INDEX": "0"} for user in users)

    with (source_dir / "sd254_users.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["Person"])
        writer.writeheader()
    return rows


def read_logical_file(artifact_dir, index, logical_name):
    path = artifact_dir / index["files"][logical_name]["path"]
    return json.loads(path.read_text(encoding="utf-8"))


def test_prepare_is_deterministic_and_freezes_exact_disjoint_cohorts(tmp_path):
    source_dir = tmp_path / "source"
    write_source(source_dir)

    first = prepare(source_dir, tmp_path / "one")
    second = prepare(source_dir, tmp_path / "two")

    assert first["bundle_id"] == second["bundle_id"]
    baseline = load_runtime(tmp_path / "one", "baseline")
    pilot = load_runtime(tmp_path / "one", "pilot")
    assert len(baseline) == 1000
    assert len({item["transaction_id"] for item in baseline}) == 1000
    assert all(item["occurred_at"].startswith("2019-") for item in baseline)
    assert len(pilot) == 20
    assert all(int(item["occurred_at"][:4]) < 2019 for item in pilot)
    assert {item["transaction_id"] for item in baseline}.isdisjoint(
        item["transaction_id"] for item in pilot
    )
    assert first["cohorts"]["baseline"]["counts"] == {
        "total": 1000,
        "fraud": 100,
        "legitimate": 900,
    }
    assert first["cohorts"]["pilot"]["counts"] == {
        "total": 20,
        "fraud": 2,
        "legitimate": 18,
    }
    assert first["history"]["covered_fraud"] == first["history"]["source_fraud"]
    assert any(item["merchant_location"]["city"] == "North\nHarbor" for item in pilot)


def test_every_source_user_has_one_tenant_and_assignment_ignores_holdout_labels(tmp_path):
    original_source = tmp_path / "original"
    changed_source = tmp_path / "changed"
    original_rows = write_source(original_source, extra_holdout=True)
    write_source(changed_source, extra_holdout=True, flip_holdout=True)

    original_index = prepare(original_source, tmp_path / "original-bundle")
    changed_index = prepare(changed_source, tmp_path / "changed-bundle")
    original = read_logical_file(tmp_path / "original-bundle", original_index, "tenant_assignments")
    changed = read_logical_file(tmp_path / "changed-bundle", changed_index, "tenant_assignments")

    expected_users = {item["User"] for item in original_rows}
    original_owners = {
        item["source_user_id"]: item["tenant_id"] for item in original["assignments"]
    }
    changed_owners = {item["source_user_id"]: item["tenant_id"] for item in changed["assignments"]}
    assert set(original_owners) == expected_users
    assert len(original_owners) == len(original["assignments"])
    assert original_owners == changed_owners


def test_future_fraud_changes_history_retention_but_never_runtime_fields(tmp_path):
    source_dir = tmp_path / "source"
    write_source(source_dir)
    path = source_dir / "credit_card_transactions-ibm_v2.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8", newline="")))
    rows[-2]["Is Fraud?"] = "Yes"
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)

    index = prepare(source_dir, tmp_path / "bundle")
    history = read_logical_file(tmp_path / "bundle", index, "history_entities")

    assert "future-only" in {item["source_user_id"] for item in history["users"]}
    for transaction in load_runtime(tmp_path / "bundle", "baseline"):
        assert "label" not in transaction
        assert "source_user_id" not in transaction

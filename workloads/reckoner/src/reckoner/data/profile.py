"""Stream aggregate readiness evidence from the simulated CCTD source files."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

TRANSACTIONS_FILE = "credit_card_transactions-ibm_v2.csv"
TRANSACTION_SUBSET_FILE = "User0_credit_card_transactions.csv"
CARDS_FILE = "sd254_cards.csv"
USERS_FILE = "sd254_users.csv"

REQUIRED_TRANSACTION_HEADERS = {
    "User",
    "Card",
    "Year",
    "Month",
    "Day",
    "Time",
    "Amount",
    "Use Chip",
    "Is Fraud?",
}


def amount_minor(raw: str) -> int:
    """Parse a formatted amount into whole minor units."""
    try:
        value = Decimal(raw.strip().removeprefix("$").replace(",", "")) * 100
    except InvalidOperation as error:
        raise ValueError("amount is not a decimal value") from error
    if not value.is_finite() or value != value.to_integral_value():
        raise ValueError("amount is not a finite whole number of cents")
    return int(value)


class _HashingReader(io.RawIOBase):
    def __init__(self, stream: Any, digest: Any) -> None:
        self._stream = stream
        self._digest = digest

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int | None:
        count = self._stream.readinto(buffer)
        if count:
            self._digest.update(memoryview(buffer)[:count])
        return count


@contextmanager
def _hashed_text(path: Path, digest: Any) -> Iterator[Any]:
    with path.open("rb") as binary_stream:
        hashing_stream = _HashingReader(binary_stream, digest)
        buffered_stream = io.BufferedReader(hashing_stream, buffer_size=1024 * 1024)
        with io.TextIOWrapper(buffered_stream, encoding="utf-8-sig", newline="") as text_stream:
            yield text_stream


def _reader(stream: Any, required_headers: set[str]) -> csv.DictReader:
    reader = csv.DictReader(stream)
    headers = set(reader.fieldnames or [])
    missing = sorted(required_headers - headers)
    if missing:
        raise ValueError(f"missing required columns: {', '.join(missing)}")
    return reader


def profile_transactions(path: Path, card_keys: set[tuple[str, str]]) -> dict[str, object]:
    """Profile transaction rows without retaining the transaction table."""
    digest = hashlib.sha256()
    row_count = 0
    legitimate_count = 0
    fraud_count = 0
    unknown_label_count = 0
    positive_amount_total_minor = 0
    invalid_amount_count = 0
    nonpositive_amount_count = 0
    negative_amount_count = 0
    zero_amount_count = 0
    dollar_prefixed_amount_count = 0
    invalid_timestamp_count = 0
    unmatched_card_count = 0
    earliest: datetime | None = None
    latest: datetime | None = None
    channels: Counter[str] = Counter()
    per_user: dict[str, dict[str, int]] = {}

    with _hashed_text(path, digest) as stream:
        reader = _reader(stream, REQUIRED_TRANSACTION_HEADERS)
        for source_row in reader:
            row_count += 1

            user = (source_row.get("User") or "").strip()
            card = (source_row.get("Card") or "").strip()
            if (user, card) not in card_keys:
                unmatched_card_count += 1

            user_summary = per_user.setdefault(user, {"total_count": 0, "fraud_count": 0})
            user_summary["total_count"] += 1

            label = (source_row.get("Is Fraud?") or "").strip()
            if label == "Yes":
                fraud_count += 1
                user_summary["fraud_count"] += 1
            elif label == "No":
                legitimate_count += 1
            else:
                unknown_label_count += 1

            raw_amount = source_row.get("Amount") or ""
            if raw_amount.strip().startswith("$"):
                dollar_prefixed_amount_count += 1
            try:
                minor = amount_minor(raw_amount)
            except ValueError:
                invalid_amount_count += 1
            else:
                if minor > 0:
                    positive_amount_total_minor += minor
                else:
                    nonpositive_amount_count += 1
                    if minor < 0:
                        negative_amount_count += 1
                    else:
                        zero_amount_count += 1

            try:
                timestamp = datetime.strptime(
                    "-".join(
                        [
                            source_row["Year"],
                            source_row["Month"],
                            source_row["Day"],
                        ]
                    )
                    + f" {source_row['Time']}",
                    "%Y-%m-%d %H:%M",
                )
            except (TypeError, ValueError):
                invalid_timestamp_count += 1
            else:
                earliest = timestamp if earliest is None or timestamp < earliest else earliest
                latest = timestamp if latest is None or timestamp > latest else latest

            channel = (source_row.get("Use Chip") or "").strip() or "<empty>"
            channels[channel] += 1

    return {
        "source_sha256": digest.hexdigest(),
        "row_count": row_count,
        "legitimate_count": legitimate_count,
        "fraud_count": fraud_count,
        "unknown_label_count": unknown_label_count,
        "positive_amount_total_minor": positive_amount_total_minor,
        "invalid_amount_count": invalid_amount_count,
        "nonpositive_amount_count": nonpositive_amount_count,
        "negative_amount_count": negative_amount_count,
        "zero_amount_count": zero_amount_count,
        "dollar_prefixed_amount_count": dollar_prefixed_amount_count,
        "invalid_timestamp_count": invalid_timestamp_count,
        "unmatched_card_count": unmatched_card_count,
        "time_range": {
            "earliest": earliest.isoformat() if earliest else None,
            "latest": latest.isoformat() if latest else None,
        },
        "channel_counts": dict(sorted(channels.items())),
        "per_user": per_user,
    }


def _profile_csv(
    path: Path,
    required_headers: set[str],
    *,
    collect_card_keys: bool = False,
) -> tuple[dict[str, object], set[tuple[str, str]], list[str]]:
    digest = hashlib.sha256()
    row_count = 0
    card_keys: set[tuple[str, str]] = set()
    with _hashed_text(path, digest) as stream:
        reader = _reader(stream, required_headers)
        headers = list(reader.fieldnames or [])
        for source_row in reader:
            row_count += 1
            if collect_card_keys:
                card_keys.add(
                    (
                        (source_row.get("User") or "").strip(),
                        (source_row.get("CARD INDEX") or "").strip(),
                    )
                )
    return (
        {
            "sha256": digest.hexdigest(),
            "byte_size": path.stat().st_size,
            "row_count": row_count,
        },
        card_keys,
        headers,
    )


def inventory(archive: Path) -> dict[str, object]:
    """Build aggregate readiness evidence for the four expected source files."""
    paths = {
        name: archive / name
        for name in (TRANSACTIONS_FILE, TRANSACTION_SUBSET_FILE, CARDS_FILE, USERS_FILE)
    }
    missing = [name for name, path in paths.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"missing source files: {', '.join(missing)}")

    card_metadata, card_keys, _ = _profile_csv(
        paths[CARDS_FILE], {"User", "CARD INDEX"}, collect_card_keys=True
    )
    user_metadata, _, user_headers = _profile_csv(paths[USERS_FILE], {"Person"})
    subset_metadata, _, _ = _profile_csv(
        paths[TRANSACTION_SUBSET_FILE], REQUIRED_TRANSACTION_HEADERS
    )
    transactions = profile_transactions(paths[TRANSACTIONS_FILE], card_keys)

    card_join_verified = transactions["unmatched_card_count"] == 0
    blockers = [
        "Source timezone is not verified by the source files or author documentation.",
        "Source currency is not verified; a dollar-sign amount format is only an observation.",
        "The users file has no explicit user identifier, so demographic enrichment is blocked.",
        "Historical availability of current-age, FICO, and dark-web fields is unverified.",
    ]
    if not card_join_verified:
        blockers.append("Some transaction user/card references do not match the cards file.")

    files = {
        TRANSACTIONS_FILE: {
            "sha256": transactions["source_sha256"],
            "byte_size": paths[TRANSACTIONS_FILE].stat().st_size,
            "row_count": transactions["row_count"],
            "included_in_main_counts": True,
        },
        TRANSACTION_SUBSET_FILE: {
            **subset_metadata,
            "included_in_main_counts": False,
        },
        CARDS_FILE: {**card_metadata, "included_in_main_counts": False},
        USERS_FILE: {**user_metadata, "included_in_main_counts": False},
    }
    return {
        "provenance": {
            "dataset": "IBM Credit Card Transactions Dataset (CCTD)",
            "simulated": True,
        },
        "files": files,
        "transactions": transactions,
        "joins": {
            "transactions_to_cards": {
                "keys": ["User", "Card/CARD INDEX"],
                "status": "verified" if card_join_verified else "unverified",
                "unmatched_transaction_rows": transactions["unmatched_card_count"],
            },
            "users": {
                "status": "unverified",
                "reason": (
                    "The users CSV contains names and attributes but no explicit user ID; "
                    "row-position identity is not assumed."
                ),
                "observed_headers": user_headers,
            },
        },
        "assumptions": {
            "timezone": "unverified",
            "currency": "unverified",
            "amount_format_observation": "Source amounts may use a leading dollar sign.",
        },
        "readiness_blockers": blockers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.output.resolve().is_relative_to(args.archive.resolve()):
            raise ValueError("output must be outside the source archive")
        report = inventory(args.archive)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except (OSError, UnicodeError, ValueError) as error:
        print(f"source inventory failed: {error}", file=sys.stderr)
        return 2

    transactions = report["transactions"]
    print(
        json.dumps(
            {
                "row_count": transactions["row_count"],
                "fraud_count": transactions["fraud_count"],
                "legitimate_count": transactions["legitimate_count"],
                "readiness_blockers": report["readiness_blockers"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

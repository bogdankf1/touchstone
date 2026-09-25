"""Prepare deterministic pilot and baseline cohorts from simulated CCTD data."""

from __future__ import annotations

import csv
import hashlib
import heapq
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from reckoner.contracts import content_id
from reckoner.data.adapter import REQUIRED_COLUMNS, adapt_row
from reckoner.data.artifacts import canonical_json, canonical_jsonl, sha256_file, verify_bundle
from reckoner.data.profile import (
    CARDS_FILE,
    TRANSACTION_SUBSET_FILE,
    TRANSACTIONS_FILE,
    USERS_FILE,
    amount_minor,
)

SEED = 20260925
SOURCE_FILES = (TRANSACTIONS_FILE, TRANSACTION_SUBSET_FILE, CARDS_FILE, USERS_FILE)
TENANTS = ("tenant-a", "tenant-b")
_STATUSES = ("eligible", "unsupported", "invalid")
LIMITS = {
    "pilot": {"fraud": 2, "legitimate": 18},
    "baseline": {"fraud": 100, "legitimate": 900},
}


@dataclass(frozen=True)
class _SourceState:
    status: str
    label: str | None
    occurred_at: datetime | None


@dataclass(frozen=True)
class _Candidate:
    key: tuple[str, str]
    transaction: dict[str, Any]
    oracle: dict[str, Any]
    source_user: str

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _Candidate):
            return NotImplemented
        return self.key > other.key


def _iter_rows(path: Path, required: set[str]) -> Iterator[tuple[int, dict[str, str]]]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            missing = sorted(required - set(reader.fieldnames or []))
            if missing:
                raise ValueError(f"missing required columns: {', '.join(missing)}")
            yield from enumerate(reader, start=1)
    except UnicodeError as error:
        raise ValueError(f"source file is not valid UTF-8: {path.name}") from error


def _source_state(row: dict[str, str]) -> _SourceState:
    missing = REQUIRED_COLUMNS - row.keys()
    if missing:
        return _SourceState("invalid", None, None)
    try:
        occurred_at = datetime.strptime(
            f"{row['Year']}-{row['Month']}-{row['Day']} {row['Time']}",
            "%Y-%m-%d %H:%M",
        )
    except (TypeError, ValueError):
        occurred_at = None
    label = {"Yes": "fraud", "No": "legitimate"}.get((row["Is Fraud?"] or "").strip())
    user = (row["User"] or "").strip()
    card = (row["Card"] or "").strip()
    merchant = (row["Merchant Name"] or "").strip()
    if label is None or not user or not card or not merchant or occurred_at is None:
        return _SourceState("invalid", label, occurred_at)
    try:
        minor = amount_minor(row["Amount"])
    except (TypeError, ValueError):
        return _SourceState("invalid", label, occurred_at)
    return _SourceState("eligible" if minor > 0 else "unsupported", label, occurred_at)


def _load_card_keys(path: Path) -> set[tuple[str, str]]:
    return {
        ((row.get("User") or "").strip(), (row.get("CARD INDEX") or "").strip())
        for _, row in _iter_rows(path, {"User", "CARD INDEX"})
    }


def _tenant_hash(user: str) -> str:
    return hashlib.sha256(f"{SEED}:tenant:{user}".encode()).hexdigest()


def _assign_tenants(users: set[str], pre_holdout: dict[str, Counter[str]]) -> dict[str, str]:
    active = [user for user in users if pre_holdout[user]["total"]]
    active.sort(
        key=lambda user: (
            -Fraction(pre_holdout[user]["fraud"], pre_holdout[user]["total"]),
            _tenant_hash(user),
            user,
        )
    )
    total = sum(pre_holdout[user]["total"] for user in active)
    prefix = 0
    best_size = 0
    best_distance = Fraction(3, 10) if total else Fraction(0)
    for size, user in enumerate(active, start=1):
        prefix += pre_holdout[user]["total"]
        distance = abs(Fraction(prefix, total) - Fraction(3, 10))
        if distance < best_distance:
            best_distance = distance
            best_size = size
    tenant_b = set(active[:best_size])
    assignments = {user: "tenant-b" if user in tenant_b else "tenant-a" for user in active}
    for user in sorted(users - set(active)):
        bucket = int(_tenant_hash(user), 16) % 10_000
        assignments[user] = "tenant-b" if bucket < 3_000 else "tenant-a"
    return assignments


def _period(occurred_at: datetime | None) -> str:
    if occurred_at is None:
        return "invalid_timestamp"
    if occurred_at.year < 2019:
        return "pre_2019"
    if occurred_at.year == 2019:
        return "holdout_2019"
    return "future_2020_plus"


def _purpose(occurred_at: datetime | None) -> str | None:
    period = _period(occurred_at)
    if period == "pre_2019":
        return "pilot"
    if period == "holdout_2019":
        return "baseline"
    return None


def _selection_key(purpose: str, transaction_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(f"{SEED}:{purpose}:{transaction_id}".encode()).hexdigest()
    return digest, transaction_id


def _add_candidate(heap: list[_Candidate], candidate: _Candidate, limit: int) -> None:
    heapq.heappush(heap, candidate)
    if len(heap) > limit:
        heapq.heappop(heap)


def _identified(document: dict[str, Any], identity_field: str) -> dict[str, Any]:
    result = dict(document)
    result[identity_field] = content_id(document)
    return result


def _account_id(tenant_id: str, source_user: str) -> str:
    return content_id(
        {
            "dataset": "cctd",
            "entity": "account",
            "tenant_id": tenant_id,
            "source_user": source_user,
        }
    )


def _manifest(
    *,
    purpose: str,
    tenant_id: str,
    cohort_id: str,
    selected: list[_Candidate],
    source_hashes: dict[str, str],
    normalization_id: str,
    source_count: int,
    retained_count: int,
    period_counts: Counter[str],
    history_entities: dict[str, int],
) -> dict[str, Any]:
    tenant_selected = [item for item in selected if item.transaction["tenant_id"] == tenant_id]
    labels = Counter(item.oracle["label"] for item in tenant_selected)
    if purpose == "baseline":
        history_end = "2018-12-31T23:59:59Z"
        evaluation_start = "2019-01-01T00:00:00Z"
        evaluation_end = "2019-12-31T23:59:59Z"
    else:
        history_end = "1990-12-31T23:59:59Z"
        evaluation_start = "1991-01-01T00:00:00Z"
        evaluation_end = "2018-12-31T23:59:59Z"
    body = {
        "schema_version": "cohort-manifest-v1",
        "tenant_id": tenant_id,
        "cohort_id": cohort_id,
        "purpose": purpose,
        "simulated": True,
        "source_hashes": source_hashes,
        "seed": SEED,
        "history_end": history_end,
        "evaluation_start": evaluation_start,
        "evaluation_end": evaluation_end,
        "inclusion_method": (
            "lowest seeded hashes of stable transaction IDs within each oracle-label stratum"
        ),
        "counts": {
            "source": source_count,
            "retained": retained_count,
            "interval_source": sum(period_counts[status] for status in _STATUSES),
            "eligible": period_counts["eligible"],
            "unsupported": period_counts["unsupported"],
            "invalid": period_counts["invalid"],
            "total": len(tenant_selected),
            "fraud": labels["fraud"],
            "legitimate": labels["legitimate"],
        },
        "history_coverage": {
            "complete_selected_histories": True,
            "users": history_entities["users"],
            "cards": history_entities["cards"],
            "merchants": history_entities["merchants"],
            "limitations": [
                "complete retained histories remain in the checksum-pinned simulated source archive"
            ],
        },
        "graph_coverage": {
            "scope": "none",
            "included_transactions": 0,
            "limitations": ["no graph is constructed for Reckoner v0"],
        },
        "dataset_normalization_version": normalization_id,
        "selected_transaction_ids": [
            item.transaction["transaction_id"] for item in tenant_selected
        ],
    }
    return _identified(body, "manifest_id")


def _write_payloads(staging: Path, payloads: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for logical_name, payload in payloads.items():
        if logical_name.startswith(("runtime_", "oracle_")):
            path = staging / f"{logical_name}.jsonl"
        else:
            path = staging / f"{logical_name}.json"
        path.write_bytes(payload)
        records = payload.count(b"\n") if path.suffix == ".jsonl" else 1
        files[logical_name] = {
            "path": path.name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "records": records,
        }
    return files


def prepare(source_dir: Path, output: Path) -> dict[str, Any]:
    """Stream the source and atomically publish deterministic cohort artifacts."""
    source_dir = Path(source_dir)
    output = Path(output)
    if output.resolve().is_relative_to(source_dir.resolve()):
        raise ValueError("output must be outside the source archive")
    missing = [name for name in SOURCE_FILES if not (source_dir / name).is_file()]
    if missing:
        raise ValueError(f"missing source files: {', '.join(missing)}")
    if output.exists():
        try:
            return verify_bundle(output, source_dir)
        except ValueError as error:
            raise ValueError("existing bundle is invalid or different") from error

    source_metadata = {
        name: {
            "sha256": sha256_file(source_dir / name),
            "byte_size": (source_dir / name).stat().st_size,
        }
        for name in SOURCE_FILES
    }
    source_sha256 = source_metadata[TRANSACTIONS_FILE]["sha256"]
    card_keys = _load_card_keys(source_dir / CARDS_FILE)

    users: set[str] = set()
    fraud_users: set[str] = set()
    pre_holdout: dict[str, Counter[str]] = {}
    source_status: Counter[str] = Counter()
    source_labels: Counter[str] = Counter()
    source_periods: Counter[str] = Counter()
    unassigned_status: Counter[str] = Counter()
    unassigned_periods: Counter[str] = Counter()
    unmatched_cards = 0
    for _, row in _iter_rows(source_dir / TRANSACTIONS_FILE, REQUIRED_COLUMNS):
        user = (row.get("User") or "").strip()
        card = (row.get("Card") or "").strip()
        raw_label = (row.get("Is Fraud?") or "").strip()
        if user:
            users.add(user)
        if raw_label == "Yes":
            source_labels["fraud"] += 1
            if user:
                fraud_users.add(user)
        elif raw_label == "No":
            source_labels["legitimate"] += 1
        if user and card and (user, card) not in card_keys:
            unmatched_cards += 1
        state = _source_state(row)
        source_status[state.status] += 1
        period = _period(state.occurred_at)
        source_periods[period] += 1
        if not user:
            unassigned_status[state.status] += 1
            unassigned_periods[period] += 1
        if user and state.occurred_at is not None and state.occurred_at.year < 2019 and state.label:
            summary = pre_holdout.setdefault(user, Counter())
            summary["total"] += 1
            summary["fraud"] += state.label == "fraud"
    if unmatched_cards:
        raise ValueError(f"unmatched transaction/card references: {unmatched_cards}")
    for user in users:
        pre_holdout.setdefault(user, Counter())
    assignments = _assign_tenants(users, pre_holdout)

    selections: dict[str, dict[str, list[_Candidate]]] = {
        purpose: {label: [] for label in limits} for purpose, limits in LIMITS.items()
    }
    period_counts: dict[str, dict[str, Counter[str]]] = {
        purpose: {tenant: Counter() for tenant in TENANTS} for purpose in LIMITS
    }
    for source_record, row in _iter_rows(source_dir / TRANSACTIONS_FILE, REQUIRED_COLUMNS):
        state = _source_state(row)
        purpose = _purpose(state.occurred_at)
        user = (row.get("User") or "").strip()
        tenant_id = assignments.get(user)
        if purpose is None or tenant_id is None:
            continue
        period_counts[purpose][tenant_id][state.status] += 1
        if state.status != "eligible" or state.label is None:
            continue
        adapted = adapt_row(
            row,
            source_sha256=source_sha256,
            source_record=source_record,
            tenant_id=tenant_id,
        )
        transaction = adapted["transaction"]
        oracle = adapted["oracle"]
        if transaction is None or oracle is None:
            raise ValueError("eligible source record did not adapt")
        candidate = _Candidate(
            key=_selection_key(purpose, transaction["transaction_id"]),
            transaction=transaction,
            oracle=oracle,
            source_user=user,
        )
        _add_candidate(selections[purpose][state.label], candidate, LIMITS[purpose][state.label])

    for purpose, limits in LIMITS.items():
        for label, limit in limits.items():
            actual = len(selections[purpose][label])
            if actual < limit:
                raise ValueError(
                    f"insufficient eligible {purpose} {label} cases: {actual} < {limit}"
                )
    selected = {
        purpose: sorted(
            [candidate for heap in by_label.values() for candidate in heap],
            key=lambda candidate: candidate.key,
        )
        for purpose, by_label in selections.items()
    }
    retained_users = fraud_users | {
        candidate.source_user for cohort in selected.values() for candidate in cohort
    }

    retained: dict[str, dict[str, Any]] = {
        user: {"records": 0, "fraud": 0, "cards": set(), "merchants": set()}
        for user in retained_users
    }
    tenant_source: Counter[str] = Counter()
    for _, row in _iter_rows(source_dir / TRANSACTIONS_FILE, REQUIRED_COLUMNS):
        user = (row.get("User") or "").strip()
        tenant_id = assignments.get(user)
        if tenant_id:
            tenant_source[tenant_id] += 1
        if user not in retained:
            continue
        summary = retained[user]
        summary["records"] += 1
        summary["fraud"] += (row.get("Is Fraud?") or "").strip() == "Yes"
        summary["cards"].add((row.get("Card") or "").strip())
        summary["merchants"].add((row.get("Merchant Name") or "").strip())

    covered_fraud = sum(summary["fraud"] for summary in retained.values())
    if covered_fraud != source_labels["fraud"]:
        raise ValueError("complete history retention does not cover every source fraud row")

    assignment_body = {
        "schema_version": "tenant-assignment-v1",
        "simulated": True,
        "seed": SEED,
        "source_file_sha256": source_sha256,
        "method": (
            "descending pre-2019 fraud fraction, seeded-hash tie break, nearest 30% volume prefix; "
            "seeded 70/30 hash assignment for users without pre-2019 activity"
        ),
        "assignments": [
            {
                "source_user_id": user,
                "tenant_id": assignments[user],
                "pre_holdout_transactions": pre_holdout[user]["total"],
                "pre_holdout_fraud": pre_holdout[user]["fraud"],
            }
            for user in sorted(assignments)
        ],
    }
    assignment_document = _identified(assignment_body, "assignment_id")

    tenant_history_entities: dict[str, dict[str, set[str]]] = {
        tenant: {"users": set(), "cards": set(), "merchants": set()} for tenant in TENANTS
    }
    tenant_retained_records: Counter[str] = Counter()
    history_users = []
    for user in sorted(retained):
        tenant_id = assignments[user]
        summary = retained[user]
        entities = tenant_history_entities[tenant_id]
        entities["users"].add(user)
        entities["cards"].update(f"{user}:{card}" for card in summary["cards"])
        entities["merchants"].update(summary["merchants"])
        tenant_retained_records[tenant_id] += summary["records"]
        history_users.append(
            {
                "source_user_id": user,
                "tenant_id": tenant_id,
                "account_id": _account_id(tenant_id, user),
                "record_count": summary["records"],
                "fraud_record_count": summary["fraud"],
            }
        )
    all_cards = {
        (tenant, card)
        for tenant, entities in tenant_history_entities.items()
        for card in entities["cards"]
    }
    all_merchants = {
        (tenant, merchant)
        for tenant, entities in tenant_history_entities.items()
        for merchant in entities["merchants"]
    }
    history_body = {
        "schema_version": "history-entities-v1",
        "simulated": True,
        "source_backing": {
            "path": TRANSACTIONS_FILE,
            "sha256": source_sha256,
            "complete_rows_are_not_copied": True,
        },
        "users": history_users,
        "counts": {
            "source_fraud": source_labels["fraud"],
            "covered_fraud": covered_fraud,
            "retained_users": len(retained),
            "retained_records": sum(summary["records"] for summary in retained.values()),
            "cards": len(all_cards),
            "merchants": len(all_merchants),
        },
        "graph_coverage": {"scope": "none", "included_transactions": 0},
    }
    history_document = _identified(history_body, "history_id")

    normalization_body = {
        "schema_version": "dataset-normalization-v1",
        "dataset": "IBM Credit Card Transactions Dataset (CCTD)",
        "simulated": True,
        "adapter_version": "cctd-adapter-v1",
        "currency": "USD",
        "source_timezone": "UTC",
        "assumptions": ["currency_assumed=USD", "source_timezone_assumed=UTC"],
    }
    normalization_document = _identified(normalization_body, "normalization_id")
    normalization_id = normalization_document["normalization_id"]
    source_hashes = {name: metadata["sha256"] for name, metadata in source_metadata.items()}

    payloads: dict[str, bytes] = {
        "tenant_assignments": canonical_json(assignment_document),
        "history_entities": canonical_json(history_document),
        "normalization": canonical_json(normalization_document),
    }
    cohort_index: dict[str, Any] = {}
    for purpose, candidates in selected.items():
        cohort_id = content_id(
            {
                "purpose": purpose,
                "seed": SEED,
                "source_file_sha256": source_sha256,
                "selected_transaction_ids": [
                    candidate.transaction["transaction_id"] for candidate in candidates
                ],
            }
        )
        runtime_name = f"runtime_{purpose}"
        oracle_name = f"oracle_{purpose}"
        payloads[runtime_name] = canonical_jsonl(
            [candidate.transaction for candidate in candidates]
        )
        payloads[oracle_name] = canonical_jsonl([candidate.oracle for candidate in candidates])
        manifest_names = []
        for tenant_id in TENANTS:
            logical_name = f"cohort_{purpose}_{tenant_id.replace('-', '_')}"
            manifest_names.append(logical_name)
            entity_counts = {
                name: len(values) for name, values in tenant_history_entities[tenant_id].items()
            }
            manifest = _manifest(
                purpose=purpose,
                tenant_id=tenant_id,
                cohort_id=cohort_id,
                selected=candidates,
                source_hashes=source_hashes,
                normalization_id=normalization_id,
                source_count=tenant_source[tenant_id],
                retained_count=tenant_retained_records[tenant_id],
                period_counts=period_counts[purpose][tenant_id],
                history_entities=entity_counts,
            )
            payloads[logical_name] = canonical_json(manifest)
        labels = Counter(candidate.oracle["label"] for candidate in candidates)
        cohort_index[purpose] = {
            "cohort_id": cohort_id,
            "runtime_file": runtime_name,
            "oracle_file": oracle_name,
            "manifest_files": manifest_names,
            "counts": {
                "total": len(candidates),
                "fraud": labels["fraud"],
                "legitimate": labels["legitimate"],
            },
        }

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        files = _write_payloads(staging, payloads)
        index_body = {
            "schema_version": "dataset-bundle-v1",
            "simulated": True,
            "seed": SEED,
            "source_files": source_metadata,
            "files": files,
            "cohorts": cohort_index,
            "history": {
                "manifest_file": "history_entities",
                "source_fraud": source_labels["fraud"],
                "covered_fraud": covered_fraud,
                "retained_users": len(retained),
                "retained_records": history_body["counts"]["retained_records"],
            },
            "tenant_assignment": {
                "manifest_file": "tenant_assignments",
                "tenant_count": 2,
                "assigned_users": len(assignments),
            },
            "normalization": {
                "manifest_file": "normalization",
                "normalization_id": normalization_id,
            },
            "source_counts": {
                "total": sum(source_status.values()),
                "eligible": source_status["eligible"],
                "unsupported": source_status["unsupported"],
                "invalid": source_status["invalid"],
                "fraud": source_labels["fraud"],
                "legitimate": source_labels["legitimate"],
                "periods": {
                    name: source_periods[name]
                    for name in (
                        "pre_2019",
                        "holdout_2019",
                        "future_2020_plus",
                        "invalid_timestamp",
                    )
                },
                "unassigned": {
                    "total": sum(unassigned_status.values()),
                    "eligible": unassigned_status["eligible"],
                    "unsupported": unassigned_status["unsupported"],
                    "invalid": unassigned_status["invalid"],
                    "periods": {
                        name: unassigned_periods[name]
                        for name in (
                            "pre_2019",
                            "holdout_2019",
                            "future_2020_plus",
                            "invalid_timestamp",
                        )
                    },
                },
            },
        }
        index = _identified(index_body, "bundle_id")
        (staging / "bundle.json").write_bytes(canonical_json(index))
        for name, metadata in source_metadata.items():
            if sha256_file(source_dir / name) != metadata["sha256"]:
                raise ValueError(f"source changed during preparation: {name}")
        verify_bundle(staging, source_dir)
        os.rename(staging, output)
        return index
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

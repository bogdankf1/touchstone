"""Verify and read immutable Reckoner dataset bundles."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from reckoner.contracts import content_id, validate_cohort_manifest, validate_document

ROOT = Path(__file__).resolve().parents[5]
SCHEMAS = ROOT / "contracts" / "schemas"


@lru_cache
def _validator(schema_name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / schema_name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def canonical_jsonl(documents: list[dict[str, Any]]) -> bytes:
    return b"".join(
        (
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        for document in documents
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid JSON artifact: {path.name}") from error
    if not isinstance(document, dict):
        raise ValueError(f"JSON artifact is not an object: {path.name}")
    return document


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    _line_number = 0
    try:
        with path.open(encoding="utf-8") as stream:
            for _line_number, line in enumerate(stream, start=1):
                document = json.loads(line)
                if not isinstance(document, dict):
                    raise ValueError
                documents.append(document)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"invalid JSONL artifact: {path.name}:{_line_number}") from error
    return documents


def _artifact_path(artifact_dir: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("artifact path escapes bundle")
    resolved = (artifact_dir / candidate).resolve()
    if not resolved.is_relative_to(artifact_dir.resolve()):
        raise ValueError("artifact path escapes bundle")
    return resolved


def _validate_identity(document: dict[str, Any], field: str, name: str) -> None:
    body = {key: value for key, value in document.items() if key != field}
    if document.get(field) != content_id(body):
        raise ValueError(f"{name} identity does not match contents")


def _verify_cohort(
    artifact_dir: Path,
    index: dict[str, Any],
    purpose: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    cohort = index["cohorts"][purpose]
    runtime = _read_jsonl(
        _artifact_path(artifact_dir, index["files"][cohort["runtime_file"]]["path"])
    )
    oracle = _read_jsonl(
        _artifact_path(artifact_dir, index["files"][cohort["oracle_file"]]["path"])
    )
    if len(runtime) != index["files"][cohort["runtime_file"]]["records"]:
        raise ValueError(f"{purpose} runtime record count mismatch")
    if len(oracle) != index["files"][cohort["oracle_file"]]["records"]:
        raise ValueError(f"{purpose} oracle record count mismatch")

    runtime_keys: set[tuple[str, str]] = set()
    runtime_ids: set[str] = set()
    for document in runtime:
        try:
            _validator("transaction-v1.schema.json").validate(document)
        except ValidationError as error:
            raise ValueError(f"invalid {purpose} runtime document") from error
        key = (document["tenant_id"], document["transaction_id"])
        if key in runtime_keys:
            raise ValueError(f"duplicate {purpose} runtime transaction")
        runtime_keys.add(key)
        if document["transaction_id"] in runtime_ids:
            raise ValueError(f"{purpose} duplicate transaction IDs across tenants")
        runtime_ids.add(document["transaction_id"])

    oracle_keys: set[tuple[str, str]] = set()
    oracle_ids: set[str] = set()
    labels = {"fraud": 0, "legitimate": 0}
    tenant_labels: dict[str, dict[str, int]] = {}
    for document in oracle:
        try:
            _validator("oracle-v1.schema.json").validate(document)
        except ValidationError as error:
            raise ValueError(f"invalid {purpose} oracle document") from error
        key = (document["tenant_id"], document["transaction_id"])
        if key in oracle_keys:
            raise ValueError(f"duplicate {purpose} oracle transaction")
        oracle_keys.add(key)
        if document["transaction_id"] in oracle_ids:
            raise ValueError(f"{purpose} duplicate transaction IDs across tenants")
        oracle_ids.add(document["transaction_id"])
        labels[document["label"]] += 1
        tenant_counts = tenant_labels.setdefault(
            document["tenant_id"], {"fraud": 0, "legitimate": 0}
        )
        tenant_counts[document["label"]] += 1
    if runtime_keys != oracle_keys:
        raise ValueError(f"{purpose} runtime/oracle membership mismatch")
    expected_counts = cohort["counts"]
    if len(runtime) != expected_counts["total"] or labels != {
        "fraud": expected_counts["fraud"],
        "legitimate": expected_counts["legitimate"],
    }:
        raise ValueError(f"{purpose} cohort count mismatch")

    selected_by_tenant: dict[str, set[str]] = {}
    manifest_class_counts: dict[str, dict[str, int]] = {}
    manifests: dict[str, dict[str, Any]] = {}
    manifest_ids: set[str] = set()
    source_hashes = {name: metadata["sha256"] for name, metadata in index["source_files"].items()}
    for logical_name in cohort["manifest_files"]:
        manifest = _read_json(_artifact_path(artifact_dir, index["files"][logical_name]["path"]))
        try:
            validate_cohort_manifest(manifest, SCHEMAS / "cohort-manifest-v1.schema.json")
        except (ValidationError, ValueError) as error:
            raise ValueError(f"invalid {purpose} cohort manifest") from error
        if manifest["purpose"] != purpose or manifest["cohort_id"] != cohort["cohort_id"]:
            raise ValueError(f"{purpose} cohort manifest identity mismatch")
        if (
            manifest["seed"] != index["seed"]
            or manifest["source_hashes"] != source_hashes
            or manifest["dataset_normalization_version"]
            != index["normalization"]["normalization_id"]
        ):
            raise ValueError(f"{purpose} cohort manifest bundle metadata mismatch")
        if manifest["tenant_id"] in selected_by_tenant:
            raise ValueError(f"duplicate {purpose} tenant manifest")
        selected_by_tenant[manifest["tenant_id"]] = set(manifest["selected_transaction_ids"])
        manifests[manifest["tenant_id"]] = manifest
        for transaction_id in manifest["selected_transaction_ids"]:
            if transaction_id in manifest_ids:
                raise ValueError(f"{purpose} duplicate transaction IDs across tenants")
            manifest_ids.add(transaction_id)
        manifest_class_counts[manifest["tenant_id"]] = {
            "fraud": manifest["counts"]["fraud"],
            "legitimate": manifest["counts"]["legitimate"],
        }
    manifest_keys = {
        (tenant_id, transaction_id)
        for tenant_id, transaction_ids in selected_by_tenant.items()
        for transaction_id in transaction_ids
    }
    if manifest_keys != runtime_keys:
        raise ValueError(f"{purpose} manifest membership mismatch")
    if set(selected_by_tenant) != {"tenant-a", "tenant-b"}:
        raise ValueError(f"{purpose} tenant manifest set mismatch")
    for tenant_id in selected_by_tenant:
        if manifest_class_counts[tenant_id] != tenant_labels.get(
            tenant_id, {"fraud": 0, "legitimate": 0}
        ):
            raise ValueError(f"{purpose} tenant class counts mismatch")
    return runtime, oracle, manifests


def _load_metadata(
    artifact_dir: Path,
    index: dict[str, Any],
    logical_name: str,
    schema_name: str,
    identity_field: str,
    description: str,
) -> dict[str, Any]:
    document = _read_json(_artifact_path(artifact_dir, index["files"][logical_name]["path"]))
    try:
        _validator(schema_name).validate(document)
    except ValidationError as error:
        raise ValueError(f"invalid {description} metadata") from error
    _validate_identity(document, identity_field, description)
    return document


def verify_bundle(artifact_dir: Path, source_dir: Path | None = None) -> dict[str, Any]:
    """Verify hashes, identities, schemas, and fixed cohort invariants."""
    artifact_dir = Path(artifact_dir)
    index = _read_json(artifact_dir / "bundle.json")
    try:
        validate_document(index, SCHEMAS / "dataset-bundle-v1.schema.json")
    except ValidationError as error:
        raise ValueError("invalid dataset bundle index") from error
    _validate_identity(index, "bundle_id", "bundle")

    expected_references = {
        index["tenant_assignment"]["manifest_file"],
        index["history"]["manifest_file"],
        index["normalization"]["manifest_file"],
    }
    document_references = set(expected_references)
    for cohort in index["cohorts"].values():
        expected_references.update(
            {cohort["runtime_file"], cohort["oracle_file"], *cohort["manifest_files"]}
        )
        document_references.update(cohort["manifest_files"])
    if (
        index["tenant_assignment"]["manifest_file"] != "tenant_assignments"
        or index["history"]["manifest_file"] != "history_entities"
        or index["normalization"]["manifest_file"] != "normalization"
        or expected_references != set(index["files"])
    ):
        raise ValueError("bundle metadata logical file references mismatch")
    if any(index["files"][name]["records"] != 1 for name in document_references):
        raise ValueError("bundle metadata artifact record counts mismatch")

    for metadata in index["files"].values():
        path = _artifact_path(artifact_dir, metadata["path"])
        if not path.is_file() or sha256_file(path) != metadata["sha256"]:
            raise ValueError(f"artifact checksum mismatch: {metadata['path']}")

    if source_dir is not None:
        source_dir = Path(source_dir)
        for name, metadata in index["source_files"].items():
            path = source_dir / name
            if not path.is_file() or sha256_file(path) != metadata["sha256"]:
                raise ValueError(f"source checksum mismatch: {name}")

    assignment_document = _load_metadata(
        artifact_dir,
        index,
        "tenant_assignments",
        "tenant-assignment-v1.schema.json",
        "assignment_id",
        "tenant assignment",
    )
    history = _load_metadata(
        artifact_dir,
        index,
        "history_entities",
        "history-entities-v1.schema.json",
        "history_id",
        "history",
    )
    normalization = _load_metadata(
        artifact_dir,
        index,
        "normalization",
        "dataset-normalization-v1.schema.json",
        "normalization_id",
        "normalization",
    )

    assignments = assignment_document["assignments"]
    users = [assignment["source_user_id"] for assignment in assignments]
    if len(users) != len(set(users)):
        raise ValueError("duplicate tenant assignment")
    if index["tenant_assignment"]["assigned_users"] != len(assignments):
        raise ValueError("bundle metadata assigned user count mismatch")
    assignment_owners = {
        assignment["source_user_id"]: assignment["tenant_id"] for assignment in assignments
    }

    if history["counts"]["covered_fraud"] != history["counts"]["source_fraud"]:
        raise ValueError("history does not cover every source fraud row")
    history_summary = {
        field: history["counts"][field]
        for field in (
            "source_fraud",
            "covered_fraud",
            "retained_users",
            "retained_records",
        )
    }
    if index["history"] != {"manifest_file": "history_entities", **history_summary}:
        raise ValueError("bundle metadata history counts mismatch")
    history_users = [user["source_user_id"] for user in history["users"]]
    if (
        len(history_users) != len(set(history_users))
        or len(history_users) != history["counts"]["retained_users"]
        or sum(user["record_count"] for user in history["users"])
        != history["counts"]["retained_records"]
        or sum(user["fraud_record_count"] for user in history["users"])
        != history["counts"]["covered_fraud"]
    ):
        raise ValueError("bundle metadata retained history counts mismatch")
    if any(
        assignment_owners.get(user["source_user_id"]) != user["tenant_id"]
        for user in history["users"]
    ):
        raise ValueError("bundle metadata history ownership mismatch")
    if index["normalization"]["normalization_id"] != normalization["normalization_id"]:
        raise ValueError("bundle metadata normalization identity mismatch")
    transaction_source = index["source_files"].get("credit_card_transactions-ibm_v2.csv")
    if (
        transaction_source is None
        or assignment_document["source_file_sha256"] != transaction_source["sha256"]
        or history["source_backing"]
        != {
            "path": "credit_card_transactions-ibm_v2.csv",
            "sha256": transaction_source["sha256"],
            "complete_rows_are_not_copied": True,
        }
    ):
        raise ValueError("bundle metadata source backing mismatch")

    baseline_runtime, _, baseline_manifests = _verify_cohort(artifact_dir, index, "baseline")
    pilot_runtime, _, pilot_manifests = _verify_cohort(artifact_dir, index, "pilot")
    if index["cohorts"]["baseline"]["counts"] != {
        "total": 1000,
        "fraud": 100,
        "legitimate": 900,
    }:
        raise ValueError("baseline must contain exactly 100 fraud and 900 legitimate cases")
    if index["cohorts"]["pilot"]["counts"] != {
        "total": 20,
        "fraud": 2,
        "legitimate": 18,
    }:
        raise ValueError("pilot must contain exactly 2 fraud and 18 legitimate cases")
    baseline_ids = {document["transaction_id"] for document in baseline_runtime}
    pilot_ids = {document["transaction_id"] for document in pilot_runtime}
    if not baseline_ids.isdisjoint(pilot_ids):
        raise ValueError("pilot and baseline cohorts overlap")

    source_counts = index["source_counts"]
    unassigned = source_counts["unassigned"]
    if (
        source_counts["eligible"] + source_counts["unsupported"] + source_counts["invalid"]
        != source_counts["total"]
        or sum(source_counts["periods"].values()) != source_counts["total"]
        or unassigned["eligible"] + unassigned["unsupported"] + unassigned["invalid"]
        != unassigned["total"]
        or sum(unassigned["periods"].values()) != unassigned["total"]
        or sum(manifest["counts"]["source"] for manifest in baseline_manifests.values())
        + unassigned["total"]
        != source_counts["total"]
    ):
        raise ValueError("source counts do not reconcile")
    for tenant_id in ("tenant-a", "tenant-b"):
        if (
            baseline_manifests[tenant_id]["counts"]["source"]
            != pilot_manifests[tenant_id]["counts"]["source"]
            or baseline_manifests[tenant_id]["counts"]["retained"]
            != pilot_manifests[tenant_id]["counts"]["retained"]
        ):
            raise ValueError("bundle metadata duplicated tenant counts mismatch")
        if (
            baseline_manifests[tenant_id]["history_coverage"]
            != pilot_manifests[tenant_id]["history_coverage"]
        ):
            raise ValueError("bundle metadata duplicated history coverage mismatch")
    if (
        sum(manifest["counts"]["retained"] for manifest in baseline_manifests.values())
        != history["counts"]["retained_records"]
    ):
        raise ValueError("bundle metadata retained record counts mismatch")
    for manifest_field, history_field in (
        ("users", "retained_users"),
        ("cards", "cards"),
        ("merchants", "merchants"),
    ):
        if (
            sum(
                manifest["history_coverage"][manifest_field]
                for manifest in baseline_manifests.values()
            )
            != history["counts"][history_field]
        ):
            raise ValueError("bundle metadata history coverage counts mismatch")
    if source_counts["fraud"] != history["counts"]["source_fraud"]:
        raise ValueError("source counts do not reconcile with history")
    for period_name, manifests in (
        ("pre_2019", pilot_manifests),
        ("holdout_2019", baseline_manifests),
    ):
        assigned_interval = sum(
            manifest["counts"]["interval_source"] for manifest in manifests.values()
        )
        if (
            assigned_interval + unassigned["periods"][period_name]
            != source_counts["periods"][period_name]
        ):
            raise ValueError("source counts do not reconcile with cohort intervals")
    return index


def load_runtime(artifact_dir: Path, purpose: str) -> list[dict[str, Any]]:
    """Return only canonical runtime transactions from a verified cohort."""
    if purpose not in {"pilot", "baseline"}:
        raise ValueError("purpose must be pilot or baseline")
    index = verify_bundle(artifact_dir)
    logical_name = index["cohorts"][purpose]["runtime_file"]
    return _read_jsonl(_artifact_path(Path(artifact_dir), index["files"][logical_name]["path"]))

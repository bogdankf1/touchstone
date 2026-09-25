import hashlib
import json

import pytest
from reckoner.cli import main
from reckoner.contracts import content_id
from reckoner.data.artifacts import canonical_json, load_runtime, verify_bundle
from reckoner.data.cohort import prepare
from test_cohort import write_source


def write_bundle_index(artifact_dir, index):
    index["bundle_id"] = content_id(
        {key: value for key, value in index.items() if key != "bundle_id"}
    )
    (artifact_dir / "bundle.json").write_bytes(canonical_json(index))


def write_json_artifact(artifact_dir, index, logical_name, document, identity_field):
    document[identity_field] = content_id(
        {key: value for key, value in document.items() if key != identity_field}
    )
    payload = canonical_json(document)
    (artifact_dir / index["files"][logical_name]["path"]).write_bytes(payload)
    index["files"][logical_name]["sha256"] = hashlib.sha256(payload).hexdigest()


def write_jsonl_artifact(artifact_dir, index, logical_name, documents):
    payload = b"".join(
        (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
        for document in documents
    )
    (artifact_dir / index["files"][logical_name]["path"]).write_bytes(payload)
    index["files"][logical_name]["sha256"] = hashlib.sha256(payload).hexdigest()


def test_verify_rejects_changed_source_bytes(tmp_path):
    source_dir = tmp_path / "source"
    write_source(source_dir)
    artifact_dir = tmp_path / "bundle"
    prepare(source_dir, artifact_dir)
    transaction_path = source_dir / "credit_card_transactions-ibm_v2.csv"

    verify_bundle(artifact_dir, source_dir)
    transaction_path.write_bytes(transaction_path.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="source checksum"):
        verify_bundle(artifact_dir, source_dir)


@pytest.mark.parametrize(
    "logical_name",
    ["runtime_baseline", "oracle_baseline", "cohort_baseline_tenant_a"],
)
def test_verify_rejects_tampered_artifacts(tmp_path, logical_name):
    source_dir = tmp_path / "source"
    write_source(source_dir)
    artifact_dir = tmp_path / "bundle"
    index = prepare(source_dir, artifact_dir)
    path = artifact_dir / index["files"][logical_name]["path"]
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(ValueError, match="artifact checksum"):
        verify_bundle(artifact_dir)


def test_verify_rejects_internally_rehashed_false_tenant_class_counts(tmp_path):
    source_dir = tmp_path / "source"
    write_source(source_dir)
    artifact_dir = tmp_path / "bundle"
    index = prepare(source_dir, artifact_dir)
    logical_name = next(
        name
        for name in index["cohorts"]["baseline"]["manifest_files"]
        if json.loads((artifact_dir / index["files"][name]["path"]).read_text())["counts"]["total"]
    )
    manifest_path = artifact_dir / index["files"][logical_name]["path"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["fraud"], manifest["counts"]["legitimate"] = (
        manifest["counts"]["legitimate"],
        manifest["counts"]["fraud"],
    )
    write_json_artifact(artifact_dir, index, logical_name, manifest, "manifest_id")
    write_bundle_index(artifact_dir, index)

    with pytest.raises(ValueError, match="tenant class counts"):
        verify_bundle(artifact_dir)


def test_verify_rejects_rehashed_transaction_id_duplicated_across_tenants(tmp_path):
    source_dir = tmp_path / "source"
    write_source(source_dir)
    artifact_dir = tmp_path / "bundle"
    index = prepare(source_dir, artifact_dir)
    runtime_name = "runtime_baseline"
    oracle_name = "oracle_baseline"
    runtime_path = artifact_dir / index["files"][runtime_name]["path"]
    oracle_path = artifact_dir / index["files"][oracle_name]["path"]
    runtime = [json.loads(line) for line in runtime_path.read_text().splitlines()]
    oracle = [json.loads(line) for line in oracle_path.read_text().splitlines()]
    by_tenant = {}
    for position, transaction in enumerate(runtime):
        by_tenant.setdefault(transaction["tenant_id"], position)
    source_position = by_tenant["tenant-a"]
    target_position = by_tenant["tenant-b"]
    source_id = runtime[source_position]["transaction_id"]
    target_id = runtime[target_position]["transaction_id"]
    runtime[target_position]["transaction_id"] = source_id
    oracle[target_position]["transaction_id"] = source_id
    target_manifest_name = "cohort_baseline_tenant_b"
    target_manifest_path = artifact_dir / index["files"][target_manifest_name]["path"]
    target_manifest = json.loads(target_manifest_path.read_text())
    target_manifest["selected_transaction_ids"] = [
        source_id if transaction_id == target_id else transaction_id
        for transaction_id in target_manifest["selected_transaction_ids"]
    ]
    write_jsonl_artifact(artifact_dir, index, runtime_name, runtime)
    write_jsonl_artifact(artifact_dir, index, oracle_name, oracle)
    write_json_artifact(artifact_dir, index, target_manifest_name, target_manifest, "manifest_id")
    write_bundle_index(artifact_dir, index)

    with pytest.raises(ValueError, match="duplicate transaction IDs"):
        verify_bundle(artifact_dir)


@pytest.mark.parametrize(
    ("logical_name", "identity_field"),
    [
        ("tenant_assignments", "assignment_id"),
        ("history_entities", "history_id"),
        ("normalization", "normalization_id"),
    ],
)
def test_verify_rejects_rehashed_extra_metadata_properties(tmp_path, logical_name, identity_field):
    source_dir = tmp_path / "source"
    write_source(source_dir)
    artifact_dir = tmp_path / "bundle"
    index = prepare(source_dir, artifact_dir)
    path = artifact_dir / index["files"][logical_name]["path"]
    document = json.loads(path.read_text())
    document["unexpected"] = True
    write_json_artifact(artifact_dir, index, logical_name, document, identity_field)
    write_bundle_index(artifact_dir, index)

    with pytest.raises(ValueError, match="invalid .* metadata"):
        verify_bundle(artifact_dir)


def test_verify_converts_malformed_metadata_to_value_error(tmp_path):
    source_dir = tmp_path / "source"
    write_source(source_dir)
    artifact_dir = tmp_path / "bundle"
    index = prepare(source_dir, artifact_dir)
    logical_name = "tenant_assignments"
    path = artifact_dir / index["files"][logical_name]["path"]
    document = json.loads(path.read_text())
    document.pop("assignments")
    write_json_artifact(artifact_dir, index, logical_name, document, "assignment_id")
    write_bundle_index(artifact_dir, index)

    with pytest.raises(ValueError, match="invalid tenant assignment metadata"):
        verify_bundle(artifact_dir)


@pytest.mark.parametrize(
    "mutation",
    [
        "assigned_users",
        "history_counts",
        "logical_reference",
        "normalization_id",
        "metadata_records",
        "source_total",
    ],
)
def test_verify_rejects_inconsistent_bundle_metadata(tmp_path, mutation):
    source_dir = tmp_path / "source"
    write_source(source_dir)
    artifact_dir = tmp_path / "bundle"
    index = prepare(source_dir, artifact_dir)
    if mutation == "assigned_users":
        index["tenant_assignment"]["assigned_users"] += 1
    elif mutation == "history_counts":
        index["history"]["retained_records"] += 1
    elif mutation == "logical_reference":
        index["history"]["manifest_file"] = "normalization"
    elif mutation == "normalization_id":
        index["normalization"]["normalization_id"] = "f" * 64
    elif mutation == "metadata_records":
        index["files"]["history_entities"]["records"] = 2
    else:
        index["source_counts"]["total"] += 1
    write_bundle_index(artifact_dir, index)

    with pytest.raises(ValueError, match="bundle metadata|source counts"):
        verify_bundle(artifact_dir)


def test_load_runtime_returns_only_canonical_transactions(tmp_path):
    source_dir = tmp_path / "source"
    write_source(source_dir)
    artifact_dir = tmp_path / "bundle"
    prepare(source_dir, artifact_dir)

    rows = load_runtime(artifact_dir, "baseline")

    assert len(rows) == 1000
    assert all(row["schema_version"] == "transaction-v1" for row in rows)
    assert all("label" not in row for row in rows)


def test_prepare_failure_publishes_no_partial_bundle(tmp_path):
    source_dir = tmp_path / "source"
    write_source(source_dir)
    transaction_path = source_dir / "credit_card_transactions-ibm_v2.csv"
    lines = transaction_path.read_text(encoding="utf-8").splitlines()
    transaction_path.write_text("\n".join(lines[:10]) + "\n", encoding="utf-8")
    output = tmp_path / "bundle"

    with pytest.raises(ValueError, match="insufficient eligible"):
        prepare(source_dir, output)

    assert not output.exists()
    assert not list(tmp_path.glob(".bundle.*"))


def test_prepare_rejects_undecodable_source_without_partial_bundle(tmp_path):
    source_dir = tmp_path / "source"
    write_source(source_dir)
    (source_dir / "credit_card_transactions-ibm_v2.csv").write_bytes(b"\xff")
    output = tmp_path / "bundle"

    with pytest.raises(ValueError, match="UTF-8"):
        prepare(source_dir, output)

    assert not output.exists()


def test_prepare_refuses_to_overwrite_a_different_bundle(tmp_path):
    source_dir = tmp_path / "source"
    write_source(source_dir)
    output = tmp_path / "bundle"
    prepare(source_dir, output)
    index_path = output / "bundle.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["seed"] = 1
    index_path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(ValueError, match="existing bundle"):
        prepare(source_dir, output)


def test_cli_prepare_and_verify_emit_safe_bundle_identity(tmp_path, capsys):
    source_dir = tmp_path / "source"
    write_source(source_dir)
    output = tmp_path / "bundle"

    assert main(["prepare", "--source-dir", str(source_dir), "--output", str(output)]) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert set(prepared) == {"bundle_id"}
    assert (
        main(
            [
                "verify",
                "--artifact-dir",
                str(output),
                "--source-dir",
                str(source_dir),
            ]
        )
        == 0
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified == prepared


def test_cli_returns_two_for_invalid_input_without_source_values(tmp_path, capsys):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    secret = "source-value-must-not-appear"
    (source_dir / "credit_card_transactions-ibm_v2.csv").write_text(secret)

    exit_code = main(
        ["prepare", "--source-dir", str(source_dir), "--output", str(tmp_path / "bundle")]
    )

    assert exit_code == 2
    assert secret not in capsys.readouterr().err

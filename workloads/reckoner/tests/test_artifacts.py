import hashlib
import json

import pytest
from reckoner.cli import main
from reckoner.contracts import content_id
from reckoner.data.artifacts import canonical_json, load_runtime, verify_bundle
from reckoner.data.cohort import prepare
from test_cohort import write_source


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
    manifest["manifest_id"] = content_id(
        {key: value for key, value in manifest.items() if key != "manifest_id"}
    )
    payload = canonical_json(manifest)
    manifest_path.write_bytes(payload)
    index["files"][logical_name]["sha256"] = hashlib.sha256(payload).hexdigest()
    index["bundle_id"] = content_id(
        {key: value for key, value in index.items() if key != "bundle_id"}
    )
    (artifact_dir / "bundle.json").write_bytes(canonical_json(index))

    with pytest.raises(ValueError, match="tenant class counts"):
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

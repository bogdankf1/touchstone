"""Generic published comparisons fail closed and do not mix generations."""

from copy import deepcopy

import pytest


def result():
    return {
        "workflow_id": "synthetic-ledger",
        "run_id": "a",
        "generation": "g",
        "refresh_state": "succeeded",
        "case_membership": [["t", "a"], ["t", "b"]],
        "reference_version": "labels-v1",
        "business_config_id": "costs-v1",
        "cohort_version": "c1",
        "currency": "USD",
        "measurement_mode": "fabricated",
        "dataset_simulated": True,
        "metrics_complete": True,
        "online_cost_complete": True,
        "correct_tasks": 2,
        "cpst": "2.5",
        "excluded_tenants": [],
        "arm_provenance": [
            {
                "config_version": "cfg",
                "model_version": "m",
                "prompt_version": "p",
                "question_version": "q",
                "calibration_id": "c",
                "evidence_version": "e",
                "retrieval_window": "30/90",
                "execution_mode": "fabricated",
                "call_ids": ["call"],
            }
        ],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("case_membership", [["t", "a"]]),
        ("case_membership", [["t", "a"], ["t", "a"]]),
        ("case_membership", [["t", "a"], ["other", "b"]]),
        ("case_membership", [["t", "a"], ["t", "b"], ["t", "extra"]]),
        ("cohort_version", "c2"),
        ("workflow_id", "other-workflow"),
        ("measurement_mode", "measured"),
        ("dataset_simulated", False),
        ("generation", "g2"),
        ("reference_version", "r2"),
        ("business_config_id", "b2"),
        ("currency", "EUR"),
        ("metrics_complete", False),
        ("online_cost_complete", False),
        ("correct_tasks", 0),
        ("arm_provenance", []),
        ("refresh_state", "failed"),
        ("excluded_tenants", ["other"]),
    ],
)
def test_comparison_blocks_ineligible_delta(field, value):
    from touchstone_platform.comparison import comparison_eligibility

    a = result()
    b = deepcopy(a)
    b[field] = value
    assert comparison_eligibility(a, b)["delta_cpst"] is None
    assert comparison_eligibility(a, b)["eligible"] is False


def test_comparison_uses_published_aggregate_cpst_and_preserves_arms():
    from touchstone_platform.comparison import comparison_eligibility

    a = result()
    b = deepcopy(a)
    b.update(run_id="b", cpst="2")
    r = comparison_eligibility(a, b)
    assert r["eligible"] is True
    assert r["delta_cpst"] == "-0.5"
    assert r["baseline"]["arm_provenance"] == a["arm_provenance"]


def attestation(declaration):
    from touchstone_platform.contracts import canonical_sha256

    comparison = {
        "reference_version": "reference",
        "business_config_id": "costs",
        "arm": {**result()["arm_provenance"][0], "config_version": declaration["config_version"]},
    }
    body = {
        "source_declaration_sha256": canonical_sha256(declaration),
        "provenance": {
            "source_manifest_sha256": declaration["replay"]["source_manifest_sha256"],
            "config_version": declaration["config_version"],
            "dataset_version": declaration["dataset_version"],
            "config_artifact_sha256": "a" * 64,
            "dataset_artifact_sha256": "b" * 64,
            "comparison_artifact_sha256": canonical_sha256(comparison),
        },
        "comparison": comparison,
    }
    return {**body, "attestation_id": canonical_sha256(body)}


@pytest.mark.parametrize("mutation", ["source", "config", "dataset", "body", "conflict", "inline"])
def test_companion_attestation_rejects_source_or_identity_conflicts(mutation):
    from touchstone_platform.comparison import resolve_comparison
    from touchstone_platform.contracts import canonical_sha256

    d = {
        "config_version": "cfg",
        "dataset_version": "data",
        "replay": {"source_manifest_sha256": "c" * 64},
    }
    a = attestation(d)
    incoming = [a]
    b = deepcopy(a)
    if mutation == "source":
        b["source_declaration_sha256"] = "d" * 64
    if mutation == "config":
        b["provenance"]["config_version"] = "other"
    if mutation == "dataset":
        b["provenance"]["dataset_version"] = "other"
    if mutation == "body":
        b["comparison"]["business_config_id"] = "other"
    if mutation == "conflict":
        b["comparison"]["business_config_id"] = "other"
        b["provenance"]["comparison_artifact_sha256"] = canonical_sha256(b["comparison"])
    if mutation == "inline":
        # Isolate the inline/attested disagreement: the inline claim keeps the declared
        # configuration and the attestation is rebuilt so every source hash still matches.
        # Control: an identical inline claim with a freshly bound attestation resolves.
        same = {**d, "comparison": a["comparison"]}
        assert resolve_comparison(same, [attestation(same)]) == a["comparison"]
        d["comparison"] = {**a["comparison"], "reference_version": "inline-reference"}
        incoming = [attestation(d)]
    if mutation != "inline":
        b["attestation_id"] = canonical_sha256(
            {k: v for k, v in b.items() if k != "attestation_id"}
        )
        incoming = [a, b] if mutation == "conflict" else [b]
    assert resolve_comparison(d, incoming) == {}


def test_old_declaration_companion_is_idempotent_and_never_rewrites_source():
    from touchstone_platform.comparison import resolve_comparison

    d = {
        "config_version": "cfg",
        "dataset_version": "data",
        "replay": {"source_manifest_sha256": "c" * 64},
    }
    original = deepcopy(d)
    a = attestation(d)
    assert resolve_comparison(d, [a, a]) == a["comparison"]
    assert d == original
    assert resolve_comparison(d, []) == {}


def test_companion_measurement_reaches_same_dbt_snapshot_without_changing_metrics(tmp_path):
    import duckdb
    from touchstone_platform.query import SnapshotReader

    from .test_metrics import declaration, event
    from .test_semantics import build_marts

    d = declaration(("a",))
    payload = attestation(d.document)
    raw = event("execution", "a").document
    raw.update(
        event_id="attestation",
        event_kind="comparison_attestation",
        payload=payload,
        trace_id="a" * 32,
        span_id="b" * 16,
    )
    # Real protobuf encoding, followed by the Collector-row extraction boundary.
    import json

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from reckoner.v1.telemetry.exporter import encode
    from touchstone_platform.extract import iter_measurements

    from .test_extract import CUTOFF, _Client, _row

    request = ExportTraceServiceRequest.FromString(encode(raw))
    encoded = request.resource_spans[0].scope_spans[0].spans[0].events[0]
    decoded = json.loads(
        next(
            a.value.string_value
            for a in encoded.attributes
            if a.key == "touchstone.measurement.json"
        )
    )
    companion = list(iter_measurements(_Client([_row(decoded)]), through=CUTOFF))[0]
    assert companion.document == raw
    warehouse = build_marts(
        tmp_path,
        [
            event("execution", "a"),
            event("outcome", "a"),
            event("provider_usage", "a"),
            companion,
            companion,
        ],
        [d],
    )
    with duckdb.connect(str(warehouse)) as c:
        reader = SnapshotReader(c, {"generation": "same"}, {"state": "succeeded"})
        arm = reader.comparison_arm("reckoner", "run-fixture-1", tenant_id="tenant-fixture-a")
        assert arm["arm_provenance"][0]["call_ids"] == ["call-a"]
        assert arm["reference_version"] == "reference"
        assert arm["cpst"] == "0.100000000000"
        assert arm["generation"] == "same"


def test_inline_provenance_cannot_override_declared_configuration():
    from touchstone_platform.comparison import resolve_comparison

    d = {"config_version": "original", "comparison": {"arm": {"config_version": "different"}}}
    assert resolve_comparison(d, []) == {}


def test_published_arms_cannot_reuse_calibration_across_changed_input():
    from touchstone_platform.comparison import comparison_eligibility

    a = result()
    b = deepcopy(a)
    b["arm_provenance"][0]["prompt_version"] = "changed"
    report = comparison_eligibility(a, b)
    assert report["eligible"] is False
    assert report["delta_cpst"] is None


def _companion(declaration_document, task="a", **comparison_changes):
    from touchstone_platform.contracts import canonical_sha256, validate_event

    from .test_metrics import RECEIVED, event

    payload = attestation(declaration_document)
    if comparison_changes:
        payload["comparison"].update(comparison_changes)
        payload["provenance"]["comparison_artifact_sha256"] = canonical_sha256(
            payload["comparison"]
        )
        payload["attestation_id"] = canonical_sha256(
            {k: v for k, v in payload.items() if k != "attestation_id"}
        )
    raw = event("execution", task).document
    raw.update(event_id="attestation", event_kind="comparison_attestation", payload=payload)
    return validate_event(raw, RECEIVED)


def test_conflicting_companion_identity_surfaces_conflict_and_blocks_comparison(tmp_path):
    import duckdb
    from touchstone_platform.query import SnapshotReader

    from .test_metrics import declaration, event
    from .test_semantics import build_marts

    d = declaration(("a",))
    original = _companion(d.document)
    conflicting = _companion(d.document, business_config_id="other-costs")
    warehouse = build_marts(
        tmp_path,
        [
            event("execution", "a"),
            event("outcome", "a"),
            event("provider_usage", "a"),
            original,
            conflicting,
        ],
        [d],
    )
    with duckdb.connect(str(warehouse)) as c:
        reader = SnapshotReader(c, {"generation": "same"}, {"state": "succeeded"})
        arm = reader.comparison_arm("reckoner", "run-fixture-1", tenant_id="tenant-fixture-a")
        assert arm["quality"]["identity_conflicts"] > 0
        assert arm["arm_provenance"] == []
        assert arm["reference_version"] is None
    from touchstone_platform.comparison import comparison_eligibility

    report = comparison_eligibility(arm, arm)
    assert report["eligible"] is False
    assert "missing arm provenance" in report["reasons"]


def test_companion_for_undeclared_task_is_rejected_not_counted_as_unexpected(tmp_path):
    import duckdb

    from .test_metrics import declaration, event
    from .test_semantics import build_marts

    d = declaration(("a",))
    warehouse = build_marts(
        tmp_path,
        [
            event("execution", "a"),
            event("outcome", "a"),
            event("provider_usage", "a"),
            _companion(d.document, task="undeclared"),
        ],
        [d],
    )
    with duckdb.connect(str(warehouse), read_only=True) as c:
        assert c.execute(
            "select count(*) from raw_measurements where event_kind='comparison_attestation'"
        ).fetchone() == (0,)
        assert c.execute("select task_id, event_name, reason from raw_rejections").fetchall() == [
            (
                "undeclared",
                "comparison_attestation",
                "comparison attestation task is not a declared root task",
            )
        ]
        assert c.execute(
            "select unexpected_tasks from mart_runs where run_id='run-fixture-1'"
        ).fetchone() == (0,)

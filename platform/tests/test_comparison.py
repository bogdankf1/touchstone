"""Generic published comparisons fail closed and do not mix generations."""

import json
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
        "declaration_versions": 1,
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
        ("declaration_versions", 2),
        ("declaration_versions", None),
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


def _companion(declaration_document, task="a", envelope=None, **comparison_changes):
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
    raw.update(envelope or {})
    return validate_event(raw, RECEIVED)


def _run_state(warehouse):
    import duckdb
    from touchstone_platform.query import SnapshotReader

    with duckdb.connect(str(warehouse)) as c:
        runs = c.execute("select * from mart_runs order by all").fetchall()
        nodes = c.execute("select * from mart_nodes order by all").fetchall()
        reader = SnapshotReader(c, {"generation": "same"}, {"state": "succeeded"})
        summary = reader.summary("reckoner", "run-fixture-1", tenant_id="tenant-fixture-a")
        arm = reader.comparison_arm("reckoner", "run-fixture-1", tenant_id="tenant-fixture-a")
    return runs, nodes, summary, arm


@pytest.mark.parametrize("scenario", ["valid", "conflict", "envelope"])
def test_attestation_never_changes_attested_run_metrics(tmp_path, scenario):
    from touchstone_platform.comparison import comparison_eligibility

    from .test_metrics import declaration, event
    from .test_semantics import build_marts

    d = declaration(("a",))
    run = [event("execution", "a"), event("outcome", "a"), event("provider_usage", "a")]
    companions = [_companion(d.document)]
    if scenario == "conflict":
        companions.append(_companion(d.document, business_config_id="other-costs"))
    if scenario == "envelope":
        companions = [_companion(d.document, envelope={"workflow_version": "other-version"})]
    plain = _run_state(build_marts(tmp_path / "plain", run, [d]))
    attested = _run_state(build_marts(tmp_path / "attested", [*run, *companions], [d]))
    assert attested[:3] == plain[:3]  # runs, nodes and summary are byte-for-byte unchanged
    assert plain[2]["metrics_complete"] is True and plain[2]["cpst"] is not None
    report = comparison_eligibility(attested[3], attested[3])
    if scenario == "valid":
        assert attested[3]["arm_provenance"][0]["call_ids"] == ["call-a"]
        assert "missing arm provenance" not in report["reasons"]
    else:
        assert attested[3]["arm_provenance"] == []
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


def test_multiple_declaration_versions_are_their_own_reason():
    from touchstone_platform.comparison import comparison_eligibility

    a = result()
    b = deepcopy(a)
    b["declaration_versions"] = 2
    assert "multiple declaration versions" in comparison_eligibility(a, b)["reasons"]


def test_comparison_arm_reports_declaration_versions(tmp_path):
    import duckdb
    from touchstone_platform.query import SnapshotReader

    from .test_metrics import declaration, event
    from .test_semantics import build_marts

    d = declaration(("a",))
    changed = declaration(("a",))
    document = {**changed.document, "code_revision": "other-revision"}
    changed = type(changed)(
        changed.identity,
        changed.trace_id,
        changed.span_id,
        changed.received_at,
        "f" * 64,
        json.dumps(document),
    )
    warehouse = build_marts(
        tmp_path, [event("execution", "a"), event("outcome", "a")], [d, changed]
    )
    with duckdb.connect(str(warehouse)) as c:
        reader = SnapshotReader(c, {"generation": "same"}, {"state": "succeeded"})
        arm = reader.comparison_arm("reckoner", "run-fixture-1", tenant_id="tenant-fixture-a")
    from touchstone_platform.comparison import comparison_eligibility

    assert arm["declaration_versions"] == 2
    assert "multiple declaration versions" in comparison_eligibility(arm, arm)["reasons"]


def test_platform_cpst_delta_keeps_full_decimal_precision():
    from touchstone_platform.comparison import comparison_eligibility

    a = result()
    b = deepcopy(a)
    a["cpst"] = "0.000000000000000000000000000001"
    b["cpst"] = "1234567890.123456789012345678901234"
    assert comparison_eligibility(a, b)["delta_cpst"] == "1234567890.123456789012345678901233999999"


def test_platform_missing_calibration_ids_are_not_reuse():
    from touchstone_platform.comparison import comparison_eligibility

    a = result()
    b = deepcopy(a)
    a["arm_provenance"][0]["calibration_id"] = None
    b["arm_provenance"][0].update(calibration_id=None, prompt_version="changed")
    reasons = comparison_eligibility(a, b)["reasons"]
    assert "missing arm provenance" in reasons
    assert "calibration reused across model-input arms" not in reasons

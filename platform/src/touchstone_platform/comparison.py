"""Comparison eligibility over declared generic provenance and governed metrics."""

from decimal import Decimal, localcontext

PINS = (
    "config_version",
    "model_version",
    "prompt_version",
    "question_version",
    "calibration_id",
    "evidence_version",
    "retrieval_window",
    "execution_mode",
    "call_ids",
)
INVARIANTS = (
    "workflow_id",
    "cohort_version",
    "reference_version",
    "business_config_id",
    "currency",
    "measurement_mode",
    "dataset_simulated",
    "generation",
)
# A changed value of any of these is a changed model input that needs its own calibration.
BINDING = (
    "model_version",
    "prompt_version",
    "question_version",
    "evidence_version",
    "retrieval_window",
)
ENVELOPE = (
    "experiment_version",
    "cohort_version",
    "config_version",
    "code_revision",
    "dataset_version",
)


def comparison_eligibility(baseline: dict, current: dict) -> dict:
    reasons = []
    for key in INVARIANTS:
        if baseline.get(key) is None or baseline.get(key) != current.get(key):
            reasons.append("incompatible " + key)
    calibrations = {}
    for arm in (baseline, current):
        for provenance in arm.get("arm_provenance", []):
            if provenance.get("calibration_id") is None:
                continue  # Missing provenance, reported below; not calibration reuse.
            binding = tuple(provenance.get(key) for key in BINDING)
            previous = calibrations.setdefault(provenance.get("calibration_id"), binding)
            if previous != binding:
                reasons.append("calibration reused across model-input arms")
    expected = sorted(baseline.get("case_membership", []))
    for arm in (baseline, current):
        members = arm.get("case_membership", [])
        if (
            not members
            or len(set(map(tuple, members))) != len(members)
            or sorted(members) != expected
        ):
            reasons.append("incompatible case membership")
        if arm.get("declaration_versions") != 1:
            reasons.append("multiple declaration versions")
        if arm.get("excluded_tenants"):
            reasons.append("excluded tenants")
        if not arm.get("arm_provenance") or any(
            any(p not in a or a[p] is None for p in PINS) for a in arm["arm_provenance"]
        ):
            reasons.append("missing arm provenance")
        if arm.get("refresh_state") != "succeeded":
            reasons.append("refresh incomplete or failed")
        if not arm.get("metrics_complete") or not arm.get("online_cost_complete"):
            reasons.append("incomplete metrics or cost")
        if not arm.get("correct_tasks") or arm.get("cpst") is None:
            reasons.append("unavailable denominator")
    delta = None
    if not reasons:
        with localcontext() as context:
            context.prec = 76  # Exact for any two DECIMAL(38, *) values.
            delta = str(Decimal(current["cpst"]) - Decimal(baseline["cpst"]))
    return {
        "eligible": not reasons,
        "reasons": sorted(set(reasons)),
        "delta_cpst": delta,
        "baseline": baseline,
        "current": current,
    }


def attestation_envelope_matches(event: dict, declaration: dict) -> bool:
    """The attestation envelope must reproduce the attested run's declared identity exactly."""
    reproducibility = event.get("reproducibility") or {}
    return (
        event.get("workflow_version") == declaration.get("workflow_version")
        and all(reproducibility.get(key) == declaration.get(key) for key in ENVELOPE)
        and event.get("simulated") is (declaration.get("measurement_mode") == "fabricated")
    )


def resolve_comparison(declaration: dict, attestations: list[dict]) -> dict:
    """Resolve content-bound claims only; external artifact authenticity is workload-owned."""
    from touchstone_platform.contracts import canonical_sha256

    inline = declaration.get("comparison", {})
    if inline and inline.get("arm", {}).get("config_version") != declaration.get("config_version"):
        return {}
    if not attestations:
        return inline
    unique = {canonical_sha256(a): a for a in attestations}
    if len(unique) != 1:
        return {}
    attestation = next(iter(unique.values()))
    body = {k: v for k, v in attestation.items() if k != "attestation_id"}
    provenance = attestation.get("provenance", {})
    comparison = attestation.get("comparison", {})
    if (
        attestation.get("attestation_id") != canonical_sha256(body)
        or attestation.get("source_declaration_sha256") != canonical_sha256(declaration)
        or provenance.get("source_manifest_sha256")
        != declaration.get("replay", {}).get("source_manifest_sha256")
        or provenance.get("config_version") != declaration.get("config_version")
        or provenance.get("dataset_version") != declaration.get("dataset_version")
        or provenance.get("comparison_artifact_sha256") != canonical_sha256(comparison)
        or comparison.get("arm", {}).get("config_version") != declaration.get("config_version")
        or (inline and inline != comparison)
    ):
        return {}
    return comparison

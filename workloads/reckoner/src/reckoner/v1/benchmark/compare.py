"""Workload arm provenance; no inference dispatch or calibration transfer."""

from copy import deepcopy
from decimal import Decimal, localcontext

PINNED = (
    "config_version",
    "model_version",
    "prompt_version",
    "question_version",
    "calibration_id",
    "evidence_version",
    "retrieval_window",
    "call_ids",
    "execution_mode",
)
INVARIANTS = (
    "workflow_id",
    "cohort_id",
    "tenant_assignment",
    "oracle_version",
    "currency",
    "business_config",
    "execution_mode",
    "dataset_simulated",
    "generation",
)
# Must match the generic platform binding: a changed retrieval window changes model input.
BINDING = (
    "model_version",
    "prompt_version",
    "question_version",
    "evidence_version",
    "retrieval_window",
)


def comparison_eligibility(baseline: dict, current: dict) -> dict:
    reasons = []
    for key in INVARIANTS:
        if baseline.get(key) is None or baseline.get(key) != current.get(key):
            reasons.append(f"incompatible {key}")
    for arm in (baseline, current):
        ids = arm.get("case_ids", [])
        if not ids or len(ids) != len(set(ids)) or set(ids) != set(baseline.get("case_ids", [])):
            reasons.append("incompatible case membership")
        if set(arm.get("tenant_assignment", {})) != set(ids):
            reasons.append("incomplete tenant assignment")
        if any(key not in arm or arm[key] is None for key in PINNED):
            reasons.append("missing arm provenance")
        if arm.get("refresh_state") != "succeeded":
            reasons.append("refresh incomplete or failed")
        if not arm.get("metrics_complete") or not arm.get("online_cost_complete"):
            reasons.append("incomplete metrics or online cost")
        if arm.get("graph_coverage") != "available":
            reasons.append("evidence coverage unavailable")
        if not arm.get("correct_tasks") or arm.get("cpst") is None:
            reasons.append("CPST denominator unavailable")
        if arm.get("membership_complete") is False:
            reasons.append("declared membership incomplete")
    return {
        "eligible": not reasons,
        "reasons": sorted(set(reasons)),
        "delta_cpst": _delta(baseline["cpst"], current["cpst"]) if not reasons else None,
    }


def _delta(baseline, current):
    with localcontext() as context:
        context.prec = 76  # Exact for any two DECIMAL(38, *) values.
        return str(Decimal(current) - Decimal(baseline))


def compare_arms(results: list[dict], expected_ids: list[str]) -> dict:
    if not expected_ids or len(expected_ids) != len(set(expected_ids)):
        raise ValueError("invalid declared cases")
    arms = deepcopy(results)
    if len({a["arm_id"] for a in arms}) != len(arms):
        raise ValueError("duplicate arm identity")
    calibrations = {}
    for arm in arms:
        arm["membership_complete"] = sorted(arm.get("case_ids", [])) == sorted(expected_ids)
        if arm.get("calibration_id") is None:
            continue  # Missing provenance, reported by eligibility; not calibration reuse.
        binding = {k: arm.get(k) for k in BINDING}
        previous = calibrations.setdefault(arm.get("calibration_id"), binding)
        if previous != binding:
            raise ValueError("calibration identity reused across model-input arms")
        if any(arm.get("calibration_binding", {}).get(k) != arm.get(k) for k in BINDING):
            raise ValueError("calibration belongs to a different model-input arm")
    return {
        "schema_version": "reckoner-comparison-v1",
        "expected_ids": expected_ids,
        "arms": arms,
        "comparisons": [
            {
                "baseline": arms[0]["arm_id"],
                "current": a["arm_id"],
                **comparison_eligibility(arms[0], a),
            }
            for a in arms[1:]
        ],
    }

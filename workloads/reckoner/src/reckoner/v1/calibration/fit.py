"""Development-only monotonic logistic candidate and independent runtime checks."""

import math
import platform
from decimal import Decimal, InvalidOperation

import numpy as np
import scipy
from scipy.optimize import minimize
from scipy.special import expit

from reckoner.contracts import content_id
from reckoner.v1.calibration.metrics import _arrays, probability

OPTIMIZER = {
    "method": "L-BFGS-B",
    "initial": [1, 0],
    "maxiter": 1000,
    "regularization": "0.001",
    "clip": "0.000001",
    "slope_minimum": 0,
}
CONTEXT_FIELDS = (
    "feature_version",
    "scaler_id",
    "graph_version",
    "retrieval_version",
    "evidence_mode",
    "data_kind",
)


def _context(context):
    try:
        scorer = {k: context["scorer"][k] for k in ("provider", "model", "question_version")}
        result = {"scorer": scorer, **{k: context[k] for k in CONTEXT_FIELDS}}
        if any(not isinstance(v, str) or not v for v in scorer.values()):
            raise ValueError("missing scorer calibration context")
        if any(not isinstance(result[k], str) or not result[k] for k in CONTEXT_FIELDS):
            raise ValueError("missing calibration context")
        if result["data_kind"] not in {"fabricated", "simulated-cctd"}:
            raise ValueError("explicit simulated or fabricated context required")
        return result
    except (KeyError, TypeError) as exc:
        raise ValueError("incomplete calibration context") from exc


def validate_sample(rows, purpose):
    if not rows:
        raise ValueError("empty calibration sample")
    _arrays(rows)
    context = _context(rows[0].get("context"))
    year = {"development": 2017, "validation": 2018}[purpose]
    strata = {}
    for row in rows:
        try:
            if row["purpose"] != purpose or row["year"] != year:
                raise ValueError(f"requires {year} {purpose} observations")
            if _context(row["context"]) != context:
                raise ValueError("mixed calibration context")
            stratum = row["stratum"]
            n, selected = stratum["N_h"], stratum["n_h"]
            if stratum["year"] != year:
                raise ValueError("cannot reuse class weights across years")
            if type(n) is not int or type(selected) is not int or not 0 < selected <= n:
                raise ValueError("invalid stratum counts")
            if Decimal(str(row["weight"])) != Decimal(n) / Decimal(selected):
                raise ValueError("weight does not match stratum counts")
            label = row["label"]
            record = {"year": year, "N_h": n, "n_h": selected}
            if label in strata and strata[label] != record:
                raise ValueError("inconsistent class stratum counts")
            strata[label] = record
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise ValueError("missing or invalid stratum counts/provenance") from exc
    for label, counts in strata.items():
        if sum(r["label"] == label for r in rows) != counts["n_h"]:
            raise ValueError("selected class count does not match frozen stratum")
    return context, {str(label): counts for label, counts in sorted(strata.items())}


def identify(artifact):
    return {**artifact, "calibration_id": content_id(artifact)}


def fit_calibration(development: list[dict]) -> dict:
    context, strata = validate_sample(development, "development")
    p, y, weights = _arrays(development)
    artifact = {
        "schema_version": "reckoner-calibration-v1",
        "context": context,
        "development_id": content_id(development),
        "strata": strata,
        "optimizer": OPTIMIZER.copy(),
        "versions": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
        "fit_status": "unavailable",
        "coefficients": None,
        "convergence": {"success": False, "iterations": 0, "message": "one_class_development"},
        "qualification": {"status": "candidate", "validation_id": None},
    }
    if len(set(y)) < 2:
        return identify(artifact)
    clipped = np.clip(p, 1e-6, 1 - 1e-6)
    logits = np.log(clipped) - np.log1p(-clipped)
    weights = weights / weights.sum()

    def objective(parameters):
        a, b = parameters
        z = a * logits + b
        error = expit(z) - y
        loss = np.dot(weights, np.logaddexp(0, z) - y * z) + 0.001 * ((a - 1) ** 2 + b**2)
        gradient = [
            np.dot(weights, error * logits) + 0.002 * (a - 1),
            np.dot(weights, error) + 0.002 * b,
        ]
        return float(loss), np.array(gradient)

    result = minimize(
        objective,
        [1.0, 0.0],
        method="L-BFGS-B",
        jac=True,
        bounds=[(0, None), (None, None)],
        options={"maxiter": 1000},
    )
    valid = np.all(np.isfinite(result.x)) and result.x[0] >= 0 and np.isfinite(result.fun)
    artifact.update(
        {
            "fit_status": "converged" if result.success and valid else "unconverged",
            "coefficients": {"a": str(float(result.x[0])), "b": str(float(result.x[1]))}
            if valid
            else None,
            "convergence": {
                "success": bool(result.success and valid),
                "iterations": int(result.nit),
                "message": str(result.message),
            },
        }
    )
    return identify(artifact)


def _validate_artifact(artifact):
    try:
        if artifact["calibration_id"] != content_id(
            {k: v for k, v in artifact.items() if k != "calibration_id"}
        ):
            raise ValueError("calibration identity mismatch")
        if artifact["schema_version"] != "reckoner-calibration-v1":
            raise ValueError("unsupported calibration artifact")
        if artifact["fit_status"] != "converged" or not artifact["convergence"]["success"]:
            raise ValueError("calibration fit is not converged")
        if artifact["optimizer"] != OPTIMIZER:
            raise ValueError("unsupported calibration optimizer")
        a, b = (float(artifact["coefficients"][k]) for k in ("a", "b"))
        if not math.isfinite(a) or not math.isfinite(b) or a < 0:
            raise ValueError("invalid calibration coefficients")
        _context(artifact["context"])
        return a, b
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("invalid calibration artifact") from exc


def apply_calibration(raw_probability: Decimal, artifact: dict | None) -> Decimal:
    probability(raw_probability)
    if artifact is None:
        return raw_probability
    a, b = _validate_artifact(artifact)
    p = np.clip(float(raw_probability), 1e-6, 1 - 1e-6)
    effective = float(expit(a * (np.log(p) - np.log1p(-p)) + b))
    return probability(str(effective))


def validate_calibration_context(artifact: dict, context: dict) -> None:
    """Must precede runtime apply; caller supplies independently pinned configuration."""
    _validate_artifact(artifact)
    try:
        qualification = artifact["qualification"]
        if qualification["status"] != "selected":
            raise ValueError("calibration candidate was not selected on validation")
        validation_id = qualification["validation_id"]
        if (
            not isinstance(validation_id, str)
            or len(validation_id) != 64
            or any(c not in "0123456789abcdef" for c in validation_id)
        ):
            raise ValueError("selected calibration requires validation identity")
        metrics = {
            k: float(qualification[k])
            for k in ("raw_brier", "candidate_brier", "raw_log_loss", "candidate_log_loss")
        }
        if (
            any(not math.isfinite(v) or v < 0 for v in metrics.values())
            or metrics["candidate_brier"] >= metrics["raw_brier"]
            or metrics["candidate_log_loss"] > metrics["raw_log_loss"]
        ):
            raise ValueError("calibration does not satisfy validation selection rule")
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("incomplete calibration qualification") from exc
    if _context(context) != _context(artifact["context"]):
        raise ValueError("calibration context does not match pinned run configuration")

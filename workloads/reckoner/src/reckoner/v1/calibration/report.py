"""Immutable evaluator-only reports from frozen labeled prediction exports."""

import json
from decimal import Decimal
from html import escape
from pathlib import Path

from reckoner.contracts import content_id
from reckoner.v1.calibration.fit import (
    apply_calibration,
    fit_calibration,
    identify,
    validate_sample,
)
from reckoner.v1.calibration.metrics import calibration_metrics


def _adjust(rows, artifact):
    return [
        {**row, "probability": str(apply_calibration(Decimal(row["probability"]), artifact))}
        for row in rows
    ]


def _prevalence(strata):
    total = sum(s["N_h"] for s in strata.values())
    return strata.get("1", {}).get("N_h", 0) / total


def _number(value):
    return "unavailable" if value is None else f"{value:.6g}"


def _markdown(report):
    lines = [
        "# Weighted scorer calibration",
        f"Data: **{report['context']['data_kind']}**, simulated; no real payment claim.",
        f"Selected score mode: **{report['score_mode']}**. Report `{report['report_id']}`.",
        "Selection requires strictly lower validation Brier and no higher log loss.",
        "Small numerical gains do not establish meaningful or population calibration.",
        "Weights correct class enrichment only within the declared retained-user population.",
        "Bootstrap uses 1,000 tenant/user clusters, seed 20260930, percentile 2.5/97.5.",
        "Empty and one-class replicates are counted and excluded from interval estimates.",
        "Fewer than two user clusters leaves intervals unavailable.",
        f"Fit status: {report['candidate']['fit_status']}; "
        f"coefficients: `{report['candidate']['coefficients']}`.",
        f"Numerical versions: `{report['candidate']['versions']}`.",
        "Sparse means fewer than 30 cases or fewer than five cases of either class.",
        "",
        "| Sample / score | Population | Cases | Brier | Log loss | ECE | Effective n | "
        "Interval status |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    reliability = []
    for sample in ("development", "validation"):
        for mode, metrics in report[sample].items():
            if metrics is None:
                lines.append(f"| {sample} / {mode} | all | unavailable | | | | | unavailable |")
                continue
            for tenant, population in [("all", metrics), *metrics["per_tenant"].items()]:
                lines.append(
                    f"| {sample} / {mode} | {escape(tenant)} | {population['counts']['cases']} | "
                    + " | ".join(
                        _number(population[k]) for k in ("brier", "log_loss", "ece", "effective_n")
                    )
                    + f" | {population['intervals']['status']} |"
                )
            reliability.extend(
                [
                    "",
                    f"## {sample} / {mode} reliability",
                    "",
                    "| Bucket | Cases / fraud / legit | Mean p | Fraud rate | Effective n | "
                    "Sparse | Intervals |",
                    "| --- | --- | ---: | ---: | ---: | --- | --- |",
                ]
            )
            for bucket in metrics["buckets"]:
                close = "]" if bucket["upper_inclusive"] else ")"
                counts = bucket["counts"]
                reliability.append(
                    f"| [{bucket['lower']}, {bucket['upper']}{close} | "
                    f"{counts['cases']} / {counts['fraud']} / {counts['legitimate']} | "
                    + " | ".join(
                        _number(bucket[k])
                        for k in ("mean_probability", "observed_rate", "effective_n")
                    )
                    + f" | {bucket['sparse']} | {bucket['intervals']['status']} |"
                )
            reliability.append("")
    lines.extend(reliability)
    lines.extend(["## Limitations", "", *[f"- {escape(w)}" for w in report["warnings"]], ""])
    return "\n".join(lines)


def _plot(report):
    from io import StringIO

    from matplotlib import rc_context
    from matplotlib.backends.backend_svg import FigureCanvasSVG
    from matplotlib.figure import Figure

    with rc_context({"svg.fonttype": "none", "svg.hashsalt": report["report_id"]}):
        figure = Figure(figsize=(6.4, 5), layout="constrained")
        canvas = FigureCanvasSVG(figure)
        axis = figure.subplots()
        axis.plot([0, 1], [0, 1], linestyle="--", color="gray", label="identity")
        for mode in ("raw", "candidate"):
            metrics = report["validation"][mode]
            if metrics is None:
                continue
            buckets = [b for b in metrics["buckets"] if b["status"] == "available"]
            axis.scatter(
                [b["mean_probability"] for b in buckets],
                [b["observed_rate"] for b in buckets],
                label=mode,
            )
        axis.set(
            xlim=(0, 1),
            ylim=(0, 1),
            xlabel="Weighted mean prediction",
            ylabel="Weighted observed fraud rate",
            title="Held-out validation reliability\n"
            + report["context"]["data_kind"]
            + "; simulated, weighted",
        )
        axis.legend()
        output = StringIO()
        canvas.print_svg(output, metadata={"Date": None})
        return output.getvalue()


def write_calibration_report(development: Path, validation: Path, output: Path) -> dict:
    """Read evaluator JSON lists; publish a new directory, never overwrite a report."""
    if output.exists():
        raise FileExistsError(output)
    if not output.parent.is_dir():
        raise ValueError("calibration output parent must exist")
    dev = json.loads(development.read_text(encoding="utf-8"))
    val = json.loads(validation.read_text(encoding="utf-8"))
    dev_context, dev_strata = validate_sample(dev, "development")
    val_context, val_strata = validate_sample(val, "validation")
    if dev_context != val_context:
        raise ValueError("development and validation context mismatch")
    if {(r["tenant_id"], r["case_id"]) for r in dev} & {
        (r["tenant_id"], r["case_id"]) for r in val
    }:
        raise ValueError("development and validation identities overlap")
    candidate = fit_calibration(dev)
    diagnostics = {
        "development": {"raw": calibration_metrics(dev), "candidate": None},
        "validation": {"raw": calibration_metrics(val), "candidate": None},
    }
    valid = candidate["fit_status"] == "converged"
    if valid:
        for name, rows in (("development", dev), ("validation", val)):
            diagnostics[name]["candidate"] = calibration_metrics(_adjust(rows, candidate))
    raw, adjusted = diagnostics["validation"]["raw"], diagnostics["validation"]["candidate"]
    selected = bool(
        valid and adjusted["brier"] < raw["brier"] and adjusted["log_loss"] <= raw["log_loss"]
    )
    validation_id = content_id(val)
    selected_artifact = None
    if selected:
        selected_artifact = identify(
            {
                **{k: v for k, v in candidate.items() if k != "calibration_id"},
                "qualification": {
                    "status": "selected",
                    "validation_id": validation_id,
                    "raw_brier": raw["brier"],
                    "candidate_brier": adjusted["brier"],
                    "raw_log_loss": raw["log_loss"],
                    "candidate_log_loss": adjusted["log_loss"],
                },
            }
        )
    before, after = _prevalence(dev_strata), _prevalence(val_strata)
    warnings = [
        "Simulated population only; class weights cannot undo entity-selection bias.",
        "Small numerical changes do not establish meaningful calibration improvement.",
        f"2017/2018 eligible prevalence: {before:.8g} -> {after:.8g}; drift {after - before:+.8g}.",
        "Some sparse reliability bins remain uncertain even when numerical selection succeeds.",
    ]
    if dev_context["data_kind"] == "fabricated":
        warnings.append(
            "Fabricated fixtures test software only; this is not real Jev calibration acceptance."
        )
    if not selected:
        warnings.append(
            "Raw probabilities retained: candidate unavailable or validation selection rule failed."
        )
    body = {
        "schema_version": "reckoner-calibration-report-v1",
        "dataset_simulated": True,
        "context": dev_context,
        "development_id": candidate["development_id"],
        "validation_id": validation_id,
        "candidate": candidate,
        "score_mode": "calibrated" if selected else "raw",
        "selected_artifact": selected_artifact,
        "strata": {"development": dev_strata, "validation": val_strata},
        "prevalence": {"2017": before, "2018": after, "difference": after - before},
        **diagnostics,
        "warnings": warnings,
    }
    report = {**body, "report_id": content_id(body)}
    output.mkdir(mode=0o700)
    for filename, contents in (
        ("report.json", json.dumps(report, indent=2, allow_nan=False) + "\n"),
        ("report.md", _markdown(report)),
        ("reliability.svg", _plot(report)),
        ("candidate.json", json.dumps(candidate, indent=2, allow_nan=False) + "\n"),
        ("selected-artifact.json", json.dumps(selected_artifact, indent=2, allow_nan=False) + "\n"),
    ):
        path = output / filename
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o600)
    return report

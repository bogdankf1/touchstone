"""Fabricated evaluator-only observations; never real scorer qualification."""

import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from reckoner.contracts import content_id


def context():
    return {
        "scorer": {"provider": "typesafe", "model": "jev-1.13.0", "question_version": "q1"},
        "feature_version": "features-v1",
        "scaler_id": "a" * 64,
        "graph_version": "graph-v1",
        "retrieval_version": "retrieval-v1",
        "evidence_mode": "relational",
        "data_kind": "fabricated",
    }


def row(p, y, w=1, case="case", user="user", tenant="tenant-a"):
    return {
        "tenant_id": tenant,
        "user_id": user,
        "case_id": case,
        "probability": str(p),
        "label": y,
        "weight": str(w),
    }


def sample(purpose="development"):
    year = 2017 if purpose == "development" else 2018
    return [
        {
            **row(p, y, case=f"{year}-{i}", user=f"user-{i // 2}"),
            "purpose": purpose,
            "year": year,
            "context": context(),
            "stratum": {"year": year, "N_h": 4, "n_h": 4},
        }
        for i, (p, y) in enumerate([(0.2, 0)] * 4 + [(0.8, 1)] * 4)
    ]


def rehash(artifact):
    artifact["calibration_id"] = content_id(
        {k: v for k, v in artifact.items() if k != "calibration_id"}
    )
    return artifact


def test_weighted_metrics_and_effective_population():
    from reckoner.v1.calibration import calibration_metrics

    metrics = calibration_metrics([row(0.2, 0, case="a"), row(0.8, 1, 3, case="b")])
    assert metrics["status"] == "available"
    assert metrics["brier"] == pytest.approx(0.04)
    assert metrics["mean_probability"] == pytest.approx(0.65)
    assert metrics["observed_rate"] == pytest.approx(0.75)
    assert metrics["effective_n"] == pytest.approx(1.6)
    assert metrics["log_loss"] == pytest.approx(0.2231435513142097)
    assert metrics["ece"] == pytest.approx(0.2)
    assert metrics["counts"] == {"cases": 2, "fraud": 1, "legitimate": 1, "users": 1}
    assert metrics["per_tenant"]["tenant-a"]["observed_rate"] == pytest.approx(0.75)


def test_bucket_edges_endpoints_and_empty_bins_are_not_zero_success():
    from reckoner.v1.calibration import calibration_metrics

    points = ["0", ".005", ".01", ".02", ".05", ".10", ".25", ".50", ".75", ".90", "1"]
    metrics = calibration_metrics([row(p, int(i == 10), case=str(i)) for i, p in enumerate(points)])
    assert [b["counts"]["cases"] for b in metrics["buckets"]] == [1] * 9 + [2]
    assert metrics["buckets"][-1]["mean_probability"] == pytest.approx(0.95)
    empty = calibration_metrics([])
    assert empty["status"] == "unavailable" and empty["brier"] is None
    assert all(
        b["status"] == "unavailable" and b["observed_rate"] is None for b in empty["buckets"]
    )
    one = calibration_metrics([row(0, 0)])
    assert one["brier"] == 0 and one["log_loss"] == pytest.approx(0.0000010000005)
    assert one["buckets"][0]["sparse"] is True
    assert one["intervals"]["status"] == "unavailable"
    assert one["bootstrap"]["one_class_replicates"] == 1000


def test_bootstrap_resamples_users_instead_of_independent_cases_and_is_seeded():
    from reckoner.v1.calibration import calibration_metrics

    rows = [row(0.1, 0, case=str(i), user="legit") for i in range(10)]
    rows += [row(0.9, 1, case="fraud", user="fraud")]
    metrics = calibration_metrics(rows)
    assert metrics == calibration_metrics(rows)
    assert metrics["bootstrap"]["replicates"] == 1000
    assert metrics["bootstrap"]["seed"] == 20260930
    assert 450 < metrics["bootstrap"]["one_class_replicates"] < 550
    assert metrics["intervals"]["observed_rate"]["lower"] == pytest.approx(1 / 11)
    assert metrics["intervals"]["observed_rate"]["upper"] == pytest.approx(1 / 11)
    separate = calibration_metrics([row(0.1, 0), row(0.9, 1, case="b", tenant="tenant-b")])
    assert set(separate["per_tenant"]) == {"tenant-a", "tenant-b"}
    assert separate["per_tenant"]["tenant-b"]["intervals"]["status"] == "unavailable"


@pytest.mark.parametrize("p", [Decimal("0"), Decimal("1"), Decimal(".123456789123456789123456789")])
def test_identity_preserves_exact_raw_decimal(p):
    from reckoner.v1.calibration import apply_calibration

    assert apply_calibration(p, None) is p


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-0.1", "1.1"])
def test_invalid_probability_rejected_even_in_raw_mode(bad):
    from reckoner.v1.calibration import apply_calibration, calibration_metrics

    with pytest.raises(ValueError):
        apply_calibration(Decimal(bad), None)
    with pytest.raises(ValueError):
        calibration_metrics([row(bad, 0)])


def test_development_fit_is_monotonic_pinned_and_does_not_mutate_inputs():
    from reckoner.v1.calibration import apply_calibration, fit_calibration

    rows = sample()
    saved = deepcopy(rows)
    artifact = fit_calibration(rows)
    assert rows == saved
    assert artifact["fit_status"] == "converged"
    assert Decimal(artifact["coefficients"]["a"]) >= 0
    assert artifact["optimizer"] == {
        "method": "L-BFGS-B",
        "initial": [1, 0],
        "maxiter": 1000,
        "regularization": "0.001",
        "clip": "0.000001",
        "slope_minimum": 0,
    }
    assert artifact["versions"]["numpy"] == "2.5.3"
    assert artifact["versions"]["scipy"] == "1.18.1"
    assert apply_calibration(Decimal(".2"), artifact) < Decimal(".2")
    assert apply_calibration(Decimal(".8"), artifact) > Decimal(".8")
    outputs = [apply_calibration(Decimal(p), artifact) for p in ["0", ".2", ".8", "1"]]
    assert outputs == sorted(outputs) and all(0 <= p <= 1 for p in outputs)
    assert artifact["calibration_id"] == content_id(
        {k: v for k, v in artifact.items() if k != "calibration_id"}
    )


@pytest.mark.parametrize("change", ["2019", "missing_counts", "wrong_year_weight", "wrong_weight"])
def test_fit_rejects_future_inputs_and_unverifiable_sampling_weights(change):
    from reckoner.v1.calibration import fit_calibration

    rows = sample()
    if change == "2019":
        rows[0]["year"] = 2019
    elif change == "missing_counts":
        del rows[0]["stratum"]["N_h"]
    elif change == "wrong_year_weight":
        rows[0]["stratum"]["year"] = 2018
    else:
        rows[0]["weight"] = "3"
    with pytest.raises(ValueError):
        fit_calibration(rows)


def test_one_class_fit_and_nonconverged_or_nan_artifacts_cannot_apply():
    from reckoner.v1.calibration import apply_calibration, fit_calibration

    rows = sample()[:4]
    fit = fit_calibration(rows)
    assert fit["fit_status"] == "unavailable" and fit["coefficients"] is None
    artifact = fit_calibration(sample())
    for field, value in [("fit_status", "unconverged"), ("coefficients", {"a": "NaN", "b": "0"})]:
        broken = rehash({**artifact, field: value})
        with pytest.raises(ValueError):
            apply_calibration(Decimal(".5"), broken)
    with pytest.raises(ValueError, match="identity"):
        apply_calibration(Decimal(".5"), {**artifact, "coefficients": {"a": "99", "b": "0"}})


def report_inputs(tmp_path):
    dev, val = tmp_path / "dev.json", tmp_path / "val.json"
    dev.write_text(json.dumps(sample()))
    val.write_text(json.dumps(sample("validation")))
    return dev, val


def test_validation_only_selects_and_reports_reproducible_candidate(tmp_path):
    from reckoner.v1.calibration import write_calibration_report

    dev, val = report_inputs(tmp_path)
    good = write_calibration_report(dev, val, tmp_path / "good")
    assert good["score_mode"] == "calibrated"
    assert good["selected_artifact"]["qualification"]["status"] == "selected"
    rows = sample("validation")
    for r in rows:
        r["label"] = 1 - r["label"]
    val.write_text(json.dumps(rows))
    bad = write_calibration_report(dev, val, tmp_path / "bad")
    assert bad["score_mode"] == "raw" and bad["selected_artifact"] is None
    assert good["candidate"] == bad["candidate"]
    assert good["validation"]["raw"] != bad["validation"]["raw"]
    assert good["development"]["raw"]["brier"] == pytest.approx(0.04)
    assert any("sparse" in warning for warning in good["warnings"])
    assert any("prevalence" in warning for warning in good["warnings"])
    assert "fabricated" in (tmp_path / "good" / "report.md").read_text()
    assert json.loads((tmp_path / "good" / "report.json").read_text()) == good
    assert "<svg" in (tmp_path / "good" / "reliability.svg").read_text()
    with pytest.raises(FileExistsError):
        write_calibration_report(dev, val, tmp_path / "good")


@pytest.mark.parametrize(
    "field",
    ["model", "question_version", "feature_version", "scaler_id", "evidence_mode", "data_kind"],
)
def test_runtime_qualification_compares_independently_pinned_context(tmp_path, field):
    from reckoner.v1.calibration import validate_calibration_context, write_calibration_report

    dev, val = report_inputs(tmp_path)
    artifact = write_calibration_report(dev, val, tmp_path / "report")["selected_artifact"]
    validate_calibration_context(artifact, context())
    requested = context()
    if field in {"model", "question_version"}:
        requested["scorer"][field] = "different"
    else:
        requested[field] = "different"
    with pytest.raises(ValueError, match="context"):
        validate_calibration_context(artifact, requested)
    missing = context()
    del missing["evidence_mode"]
    with pytest.raises(ValueError):
        validate_calibration_context(artifact, missing)


def test_unselected_candidate_cannot_qualify_for_runtime():
    from reckoner.v1.calibration import fit_calibration, validate_calibration_context

    with pytest.raises(ValueError, match="selected"):
        validate_calibration_context(fit_calibration(sample()), context())


def test_calibration_cli_publishes_fabricated_report_without_environment(tmp_path, capsys):
    from reckoner.cli import main

    dev, val = report_inputs(tmp_path)
    assert (
        main(
            [
                "v1",
                "calibrate",
                "--development",
                str(dev),
                "--validation",
                str(val),
                "--output",
                str(tmp_path / "cli"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["score_mode"] == "calibrated"
    assert (Path(tmp_path) / "cli" / "report.md").exists()


def test_runtime_rejects_selection_without_numeric_validation_qualification(tmp_path):
    from reckoner.v1.calibration import validate_calibration_context, write_calibration_report

    dev, val = report_inputs(tmp_path)
    artifact = write_calibration_report(dev, val, tmp_path / "report")["selected_artifact"]
    for key, value in [("raw_brier", -1), ("candidate_log_loss", 999), ("validation_id", None)]:
        broken = deepcopy(artifact)
        broken["qualification"][key] = value
        rehash(broken)
        with pytest.raises(ValueError):
            validate_calibration_context(broken, context())
    broken = deepcopy(artifact)
    del broken["qualification"]
    rehash(broken)
    with pytest.raises(ValueError):
        validate_calibration_context(broken, context())


def test_log_loss_guard_retains_raw_despite_lower_validation_brier(tmp_path):
    from reckoner.v1.calibration import write_calibration_report

    dev, val = report_inputs(tmp_path)
    rows = []
    for i in range(24):
        label = int(i >= 12)
        p = 0.8 if label else 0.2
        if i in {0, 12}:
            p = 1 - p
        rows.append(
            {
                **sample("validation")[0],
                **row(p, label, case=f"2018-{i}", user=f"val-user-{i}"),
                "stratum": {"year": 2018, "N_h": 12, "n_h": 12},
            }
        )
    val.write_text(json.dumps(rows))
    report = write_calibration_report(dev, val, tmp_path / "guard")
    raw, adjusted = report["validation"]["raw"], report["validation"]["candidate"]
    assert adjusted["brier"] < raw["brier"]
    assert adjusted["log_loss"] > raw["log_loss"]
    assert report["score_mode"] == "raw" and report["selected_artifact"] is None


def test_optimizer_nonconvergence_retains_raw_and_exposes_failure(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import numpy as np
    from reckoner.v1.calibration import fit, write_calibration_report

    # The numeric optimizer boundary is forced to fail; report behavior stays real.
    monkeypatch.setattr(
        fit,
        "minimize",
        lambda *args, **kwargs: SimpleNamespace(
            x=np.array([1.0, 0.0]), success=False, fun=0.1, nit=1000, message="iteration_limit"
        ),
    )
    dev, val = report_inputs(tmp_path)
    report = write_calibration_report(dev, val, tmp_path / "failed")
    assert report["candidate"]["fit_status"] == "unconverged"
    assert report["candidate"]["convergence"]["iterations"] == 1000
    assert report["validation"]["candidate"] is None
    assert report["score_mode"] == "raw"


def test_weighted_fitting_changes_candidate_and_rejects_incomplete_sample():
    from reckoner.v1.calibration import fit_calibration

    unweighted = fit_calibration(sample())
    rows = sample()
    for r in rows:
        if r["label"] == 0:
            r["weight"] = "10"
            r["stratum"]["N_h"] = 40
    weighted = fit_calibration(rows)
    assert Decimal(weighted["coefficients"]["b"]) < Decimal(unweighted["coefficients"]["b"])
    with pytest.raises(ValueError, match="selected class count"):
        fit_calibration(rows[:-1])


def test_report_rejects_validation_training_year_and_weight_reuse(tmp_path):
    from reckoner.v1.calibration import write_calibration_report

    dev, val = report_inputs(tmp_path)
    rows = sample("validation")
    rows[0]["stratum"]["year"] = 2017
    val.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="across years"):
        write_calibration_report(dev, val, tmp_path / "wrong")
    assert not (tmp_path / "wrong").exists()


def test_single_user_cannot_estimate_cluster_uncertainty():
    from reckoner.v1.calibration import calibration_metrics

    metrics = calibration_metrics([row(0.2, 0, case="a"), row(0.8, 1, case="b")])
    assert metrics["intervals"]["status"] == "unavailable"
    assert metrics["intervals"]["reason"] == "insufficient_user_clusters"
    assert metrics["bootstrap"]["estimable_replicates"] == 0


def test_reliability_bucket_assignment_preserves_decimal_boundary_precision():
    from reckoner.v1.calibration import calibration_metrics

    metrics = calibration_metrics(
        [
            row(".0049999999999999999999999999999", 0, case="below"),
            row(".0050000000000000000000000000000", 1, case="equal"),
        ]
    )
    assert [b["counts"]["cases"] for b in metrics["buckets"]][:2] == [1, 1]


def test_standalone_plot_uses_standard_producer_and_is_reproducible(tmp_path):
    import xml.etree.ElementTree as ET

    from reckoner.v1.calibration import write_calibration_report

    dev, val = report_inputs(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    write_calibration_report(dev, val, first)
    write_calibration_report(dev, val, second)
    svg = (first / "reliability.svg").read_text()
    root = ET.fromstring(svg)
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert "Matplotlib" in " ".join(root.itertext())
    assert "fabricated" in " ".join(root.itertext())
    assert svg == (second / "reliability.svg").read_text()

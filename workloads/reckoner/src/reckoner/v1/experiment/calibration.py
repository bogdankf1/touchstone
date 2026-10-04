"""Privileged calibration export and owner-side selected-artifact registration.

The exporter is an owner/offline evaluator path: it reads the frozen Task 2 sample
(runtime, oracle, strata) and the persisted successful score records of one
executed protocol, and joins them on tenant and transaction identity. It refuses
duplicate, missing or extra identities, a different protocol membership, unresolved
cutoffs, changed scorer/question/evidence context or invalid labels. Oracle joins
never pass through the runner, reviewer API or browser. Simulated data only.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from reckoner.contracts import content_id
from reckoner.v1.data.history import instant
from reckoner.v1.experiment.protocol import validate_body

LABELS = {"fraud": 1, "legitimate": 0}


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line]


def load_frozen_sample(bundle: Path, purpose: str) -> dict:
    """The checksum-verified frozen sample, its runtime rows and evaluator oracle rows."""
    from reckoner.v1.data.prepare import verify_preparation

    bundle = Path(bundle)
    index = verify_preparation(bundle)
    return {
        "bundle_id": index["bundle_id"],
        "sample": index["samples"][purpose],
        "runtime": _lines(bundle / index["files"][f"runtime_{purpose}"]["path"]),
        "oracle": _lines(bundle / index["files"][f"oracle_{purpose}"]["path"]),
    }


def _unique(rows, what):
    keyed = {}
    for row in rows:
        key = (row["tenant_id"], row["transaction_id"])
        if key in keyed:
            raise ValueError(f"duplicate {what} identity")
        keyed[key] = row
    return keyed


def calibration_context(repo, protocol: dict, data_kind: str) -> dict:
    """Context established from pinned run configurations and the protocol, never an artifact."""
    if data_kind not in {"fabricated", "simulated-cctd"}:
        raise ValueError("explicit simulated or fabricated data kind required")
    modes = {case["evidence_mode"] for case in protocol["cases"]}
    if len(modes) != 1:
        raise ValueError("calibration cannot mix evidence modes")
    contexts = set()
    for run in protocol["runs"]:
        config = repo.workflow_document(
            "v1_configs", {"tenant_id": run["tenant_id"], "config_id": run["config_id"]}
        )
        scorer = config["scorer"]
        contexts.add(
            json.dumps(
                {
                    "scorer": {k: scorer[k] for k in ("provider", "model", "question_version")},
                    **{
                        k: config[k]
                        for k in (
                            "feature_version",
                            "scaler_id",
                            "graph_version",
                            "retrieval_version",
                        )
                    },
                },
                sort_keys=True,
            )
        )
    if len(contexts) != 1:
        raise ValueError("runs disagree on the scorer/feature/scaler calibration context")
    context = json.loads(contexts.pop())
    if context["scorer"]["model"] != protocol["model"] or (
        context["scorer"]["question_version"] != protocol["versions"].get("question_version")
    ):
        raise ValueError("pinned scorer differs from the executed protocol")
    if not isinstance(context["scaler_id"], str):
        raise ValueError("calibration context requires a pinned frozen scaler")
    return {**context, "evidence_mode": modes.pop(), "data_kind": data_kind}


def export_calibration_rows(
    repo,
    *,
    frozen: dict,
    protocol: dict,
    data_kind: str,
    expected_scorer: dict | None = None,
    evidence_mode: str | None = None,
) -> dict:
    """Task 5 evaluator rows for one executed protocol, with provenance hashes."""
    validate_body(protocol)
    sample = frozen["sample"]
    purpose, year = sample["purpose"], sample["year"]
    if purpose not in {"development", "validation"} or protocol["purpose"] != purpose:
        raise ValueError("protocol purpose differs from the frozen calibration sample")
    runtime = _unique(frozen["runtime"], "runtime")
    oracle = _unique(frozen["oracle"], "oracle")
    selected = sample["selected_transaction_ids"]
    if (
        len(set(selected)) != len(selected)
        or {k[1] for k in runtime} != set(selected)
        or len(runtime) != len(selected)
        or set(oracle) != set(runtime)
    ):
        raise ValueError("frozen sample, runtime and oracle membership differ")
    cases = {(c["tenant_id"], c["transaction_id"]): c for c in protocol["cases"]}
    if set(cases) != set(runtime):
        raise ValueError("executed protocol membership differs from the frozen sample")
    context = calibration_context(repo, protocol, data_kind)
    if evidence_mode is not None and evidence_mode != context["evidence_mode"]:
        raise ValueError("requested evidence mode differs from the executed protocol")
    if expected_scorer is not None and expected_scorer != context["scorer"]:
        raise ValueError("requested scorer differs from the pinned scorer")
    requests = {
        (d["tenant_id"], d["run_id"], t["task_id"]): t["request_sha256"]
        for d in protocol["dispatch"]
        for t in d["tasks"]
    }
    calls = repo._connection.execute(
        "SELECT c.tenant_id,c.run_id,c.task_id,c.call_id,r.score "
        "FROM reckoner.v1_provider_calls c JOIN reckoner.v1_provider_responses r USING (call_id) "
        "WHERE c.protocol_id = ANY(%s) ORDER BY c.dispatched_at,c.call_id",
        ([d["protocol_id"] for d in protocol["dispatch"]],),
    ).fetchall()
    latest = {}
    for call in calls:
        latest[(call["tenant_id"], call["run_id"], call["task_id"])] = call
    cutoff = datetime(year + 1, 1, 1, tzinfo=UTC)
    rows, call_ids = [], []
    for key, case in sorted(cases.items()):
        task_key = (case["tenant_id"], case["run_id"], case["task_id"])
        call = latest.get(task_key)
        score = call["score"] if call else None
        if (
            score is None
            or score.get("attempt_status") != "responded"
            or score["requested_model"] != context["scorer"]["model"]
            or score["reported_model"] != context["scorer"]["model"]
            or score["question_version"] != context["scorer"]["question_version"]
            or score["input_sha256"] != requests[task_key]
            or score["tenant_id"] != case["tenant_id"]
        ):
            raise ValueError("a frozen case lacks an authentic successful pinned score")
        transaction = runtime[key]
        occurred = instant(transaction["occurred_at"])
        if occurred.year != year or occurred + timedelta(days=7) >= cutoff:
            raise ValueError("a case does not resolve strictly before the sample cutoff")
        label = LABELS.get(oracle[key].get("label"))
        if label is None:
            raise ValueError("invalid evaluator label")
        name = oracle[key]["label"]
        stratum = sample["strata"][name]
        if Decimal(stratum["weight"]) != Decimal(stratum["N_h"]) / Decimal(stratum["n_h"]):
            raise ValueError("frozen stratum weight does not match its counts")
        call_ids.append(call["call_id"])
        rows.append(
            {
                "tenant_id": case["tenant_id"],
                "user_id": transaction["account_id"],
                "case_id": case["transaction_id"],
                "probability": score["raw_probability"],
                "label": label,
                "weight": stratum["weight"],
                "purpose": purpose,
                "year": year,
                "stratum": {"year": year, "N_h": stratum["N_h"], "n_h": stratum["n_h"]},
                "context": context,
            }
        )
    provenance = {
        "schema_version": "reckoner-calibration-export-v1",
        "dataset_simulated": True,
        "data_kind": data_kind,
        "protocol_sha256": protocol["protocol_sha256"],
        "bundle_id": frozen["bundle_id"],
        "sample_id": sample["sample_id"],
        "score_call_ids_sha256": content_id(sorted(call_ids)),
        "rows_sha256": content_id(rows),
        "row_count": len(rows),
    }
    return {"rows": rows, "provenance": provenance}


def register_selected(
    repo,
    *,
    report_dir: Path,
    tenants: list[str],
    context: dict,
    development_rows: list[dict],
    validation_rows: list[dict],
) -> str:
    """Owner-side registration of a Task 5 selected artifact for each tenant.

    The artifact must have been fitted on exactly the exported development rows and
    qualified on exactly the exported validation rows of the recorded protocols.
    """
    from reckoner.v1.storage.configurations import register_calibration

    artifact = json.loads((Path(report_dir) / "selected-artifact.json").read_text())
    if artifact is None:
        raise ValueError("raw scores retained; no selected calibration to register")
    if artifact.get("development_id") != content_id(development_rows):
        raise ValueError("artifact was not fitted on the exported development rows")
    if (artifact.get("qualification") or {}).get("validation_id") != content_id(validation_rows):
        raise ValueError("artifact was not qualified on the exported validation rows")
    for tenant in tenants:
        register_calibration(repo, tenant_id=tenant, artifact=artifact, context=context)
    return artifact["calibration_id"]

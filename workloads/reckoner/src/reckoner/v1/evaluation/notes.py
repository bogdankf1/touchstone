"""Evaluator-owned oracle join; judges receive only their documented inputs."""

import json
import math
from copy import deepcopy

from reckoner.v1.evaluation.report import aggregate, persist_report
from reckoner.v1.notes.calls import ApprovalRequired
from reckoner.v1.notes.validate import CONTENT_FIELDS, validate_note
from reckoner.v1.storage.budget import ProviderBudget, validate_protocol


def evaluate_note_fixtures(
    cases: list[dict],
    judges: dict,
    expected_count: int,
    *,
    protocol: dict | None = None,
    budget=None,
) -> dict:
    if type(expected_count) is not int or expected_count < 0 or len(cases) > expected_count:
        raise ValueError("invalid expected case population")
    if len({(c["tenant_id"], c["case_id"]) for c in cases}) != len(cases):
        raise ValueError("duplicate expected case")
    authorized = protocol is not None and protocol.get("approved") is True and budget is not None
    if authorized and isinstance(budget, ProviderBudget):
        stages = list(protocol.get("stages", {}).values()) or [protocol]
        for stage in stages:
            validate_protocol(stage)
        scopes = {(stage["tenant_id"], stage["run_id"]) for stage in stages}
        if len(scopes) != 1:
            raise ValueError("evaluation population must belong to one run")
        tenant, run = scopes.pop()
        pending = budget._connection.execute(
            "SELECT count(*) AS n FROM reckoner.v1_tasks WHERE tenant_id=%s "
            "AND run_id=%s AND status<>'completed'",
            (tenant, run),
        ).fetchone()["n"]
        expected = budget._connection.execute(
            "SELECT tenant_id,decision_id AS case_id FROM "
            "reckoner.v1_decisions WHERE tenant_id=%s AND run_id=%s AND "
            "outcome='escalate'",
            (tenant, run),
        ).fetchall()
        if (
            pending
            or {(r["tenant_id"], r["case_id"]) for r in expected}
            != {(c["tenant_id"], c["case_id"]) for c in cases}
            or len(expected) != expected_count
        ):
            raise ValueError(
                "evaluation population must include every declared escalation after root completion"
            )
    rows = []
    for case in cases:
        row = {k: case[k] for k in ("tenant_id", "case_id")}
        row.update(
            schema="fail", status="not-evaluated", agreement=None, faithfulness=None, judges={}
        )
        rows.append(row)
        try:
            validate_note(case["note"], case["evidence"], case["score"])
            if any(case["note"][k] != case[k] for k in ("tenant_id", "case_id")):
                raise ValueError("note case identity mismatch")
        except (ValueError, KeyError, TypeError):
            row["status"] = "fail"
            continue
        row["schema"] = "pass"
        row["note_id"] = case["note"]["note_id"]
        row["evidence_id"] = case["evidence"]["evidence_id"]
        if not authorized:
            continue
        note = {k: deepcopy(case["note"][k]) for k in CONTENT_FIELDS}
        current = {}
        try:
            identity = {k: case[k] for k in ("tenant_id", "case_id")}
            current = {
                key: judge(identity) if callable(judge) else judge for key, judge in judges.items()
            }
            if case.get("oracle_verdict") not in {"approve", "decline"}:
                raise ValueError("missing evaluator oracle")
            for judge in current.values():
                if hasattr(judge, "protocol") and judge.protocol != protocol:
                    raise ValueError("judge protocol differs from evaluation approval")
                if (
                    hasattr(judge, "calls")
                    and hasattr(judge.calls, "budget")
                    and judge.calls.budget is not budget
                ):
                    raise ValueError("judge must share the injected reservation implementation")
            verdict_judge = current["verdict"]
            if hasattr(verdict_judge, "measure"):
                from deepeval.test_case import LLMTestCase

                verdict_judge.measure(
                    LLMTestCase(
                        input="Read only the simulated note.",
                        actual_output=json.dumps(note, sort_keys=True),
                        expected_output=case["oracle_verdict"],
                    )
                )
                verdict = verdict_judge.last_verdict
            else:
                verdict = verdict_judge.evaluate(note)
            if verdict not in {"approve", "decline"}:
                raise ValueError("invalid judge verdict")
            row["agreement"] = verdict == case["oracle_verdict"]
            row["judge_verdict"] = verdict
            faith = current["faithfulness"].evaluate(
                note, evidence=deepcopy(case["evidence"]), score=deepcopy(case["score"])
            )
            if (
                isinstance(faith, bool)
                or not isinstance(faith, (float, int))
                or not math.isfinite(faith)
                or not 0 <= faith <= 1
            ):
                raise ValueError("faithfulness unavailable")
            row["faithfulness"] = faith
            row["status"] = "pass" if row["agreement"] and faith >= 0.9 else "fail"
        except ApprovalRequired as pending:
            row["status"] = "not-evaluated"
            row["pending_stage"] = {
                "stage": pending.stage,
                "request_sha256": pending.request_sha256,
                "request": pending.request,
            }
        except Exception:
            row["status"] = "error"
            row["error"] = "evaluation unavailable"
        row["judges"] = {key: deepcopy(judge.provenance) for key, judge in current.items()}
    report = aggregate(rows, expected_count, authorized)
    return persist_report(budget, protocol, report) if authorized else report

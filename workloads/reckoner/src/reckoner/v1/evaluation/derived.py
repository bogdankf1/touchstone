"""Versioned derived-request policy for bounded judge chains (`derived-request-v1`).

A judge stage whose input depends on an earlier response cannot be hashed before
that response exists. Inside one approved, bounded protocol such a request is
allowed only when it is exactly reconstructed, before dispatch, from a fixed
builder/template/library version and the persisted parent response for the same
frozen case. No arbitrary prompt, extra stage, case, model or attempt is admitted.
"""

import json
from importlib.metadata import version

from reckoner.contracts import content_id
from reckoner.v1.notes.generate import parse_json
from reckoner.v1.notes.prompt import build_note_context

POLICY = "derived-request-v1"
# builder -> (stage, parent stage, library)
BUILDERS = {
    "judge-verdict-v1": ("judge-verdict", "note", "deepeval"),
    "ragas-statements-v1": ("judge-statements", "note", "ragas"),
    "ragas-nli-v1": ("judge-faithfulness", "judge-statements", "ragas"),
}
PROBE = {
    "verdict_recommendation": "approve",
    "confidence": {"value": None, "meaning": "unavailable"},
    "risk_indicators": [],
    "entity_neighbourhood": {"summary": "probe", "evidence_refs": []},
    "comparable_cases": [],
    "what_would_change_verdict": [],
}


def _render(builder, note, *, statements=None, context=None):
    from ragas.metrics.collections.faithfulness.util import (
        NLIStatementInput,
        NLIStatementPrompt,
        StatementGeneratorInput,
        StatementGeneratorPrompt,
    )

    from reckoner.v1.evaluation.judges import (
        RAGAS_QUESTION,
        RAGAS_SYSTEM,
        VERDICT_SYSTEM,
        note_text,
    )

    if builder == "judge-verdict-v1":
        return VERDICT_SYSTEM, note_text(note)
    if builder == "ragas-statements-v1":
        prompt = StatementGeneratorPrompt().to_string(
            StatementGeneratorInput(question=RAGAS_QUESTION, answer=note_text(note))
        )
        return RAGAS_SYSTEM, prompt
    if builder == "ragas-nli-v1":
        prompt = NLIStatementPrompt().to_string(
            NLIStatementInput(
                context="\n".join([json.dumps(context, sort_keys=True)]), statements=statements
            )
        )
        return RAGAS_SYSTEM, prompt
    raise ValueError("unknown derived-request builder")


def template_sha256(builder: str) -> str:
    """Identity of the fixed system text and library prompt template for a builder."""
    if builder not in BUILDERS:
        raise ValueError("unknown derived-request builder")
    rendered = _render(builder, PROBE, statements=["probe statement"], context={"probe": "context"})
    return content_id({"builder": builder, "system": rendered[0], "probe_prompt": rendered[1]})


def derivation(builder: str) -> dict:
    """The exact policy block a dispatch protocol pins for one derived stage."""
    stage, parent, library = BUILDERS[builder]
    return {
        "policy_version": POLICY,
        "stage": stage,
        "builder": builder,
        "parent_stage": parent,
        "template_sha256": template_sha256(builder),
        "library": library,
        "library_version": version(library),
    }


def _persisted(repo, task, config):
    decision = repo.workflow_document(
        "v1_decisions", {k: task[k] for k in ("tenant_id", "run_id", "task_id")}
    )
    if decision is None or decision["outcome"] != "escalate":
        raise ValueError("derived judge request needs a persisted escalation")
    result = repo.workflow_document(
        "v1_note_results", {"tenant_id": task["tenant_id"], "case_id": decision["decision_id"]}
    )
    if result is None or result["status"] != "succeeded" or result.get("note") is None:
        raise ValueError("derived judge request needs a persisted successful note parent")
    note_call = repo._connection.execute(
        "SELECT c.call_id, r.body FROM reckoner.v1_provider_calls c "
        "JOIN reckoner.v1_provider_responses r USING (call_id) "
        "JOIN reckoner.v1_settlements s ON s.call_id=c.call_id AND s.status='settled' "
        "WHERE c.tenant_id=%s AND c.call_id=%s",
        (task["tenant_id"], result["note"]["call_id"]),
    ).fetchone()
    if note_call is None:
        raise ValueError("derived parent note response is missing or unsettled")
    return decision, result["note"], note_call


def expected_request(repo, task: dict, config: dict, policy: dict) -> tuple[dict, dict]:
    """Rebuild the one admissible request for this case/stage, plus its lineage."""
    from reckoner.v1.evaluation.judges import request

    builder = policy["builder"]
    if builder not in BUILDERS or policy != derivation(builder):
        raise ValueError("derived-request policy, template or library version changed")
    decision, note, note_call = _persisted(repo, task, config)
    parent = {"stage": "note", "call_id": note_call["call_id"]}
    parent["response_sha256"] = content_id(note_call["body"])
    statements = context = None
    if policy["parent_stage"] == "judge-statements":
        row = repo._connection.execute(
            "SELECT g.call_id, r.body, r.score FROM reckoner.v1_generation_stages g "
            "JOIN reckoner.v1_provider_responses r USING (call_id) "
            "JOIN reckoner.v1_settlements s ON s.call_id=g.call_id AND s.status='settled' "
            "WHERE g.tenant_id=%s AND g.run_id=%s AND g.task_id=%s AND g.stage=%s",
            (task["tenant_id"], task["run_id"], task["task_id"], "judge-statements"),
        ).fetchone()
        if row is None or row["score"].get("status") != "responded" or row["body"] is None:
            raise ValueError("derived parent judge-statements response is missing")
        from ragas.metrics.collections.faithfulness.util import StatementGeneratorOutput

        statements = StatementGeneratorOutput.model_validate(
            parse_json(row["body"].get("content"))
        ).statements
        if not statements:
            raise ValueError("claim-free parent cannot derive a faithfulness request")
        evidence = repo.workflow_document(
            "v1_evidence", {"tenant_id": task["tenant_id"], "evidence_id": decision["evidence_id"]}
        )
        score = {}
        if decision["call_id"] is not None:
            score = repo._connection.execute(
                "SELECT score FROM reckoner.v1_provider_responses WHERE tenant_id=%s "
                "AND call_id=%s",
                (task["tenant_id"], decision["call_id"]),
            ).fetchone()["score"]
        context = build_note_context(decision, evidence, score, config)
        parent = {
            "stage": "judge-statements",
            "call_id": row["call_id"],
            "response_sha256": content_id(row["body"]),
        }
    system, data = _render(builder, note, statements=statements, context=context)
    return request(config, system, data), parent

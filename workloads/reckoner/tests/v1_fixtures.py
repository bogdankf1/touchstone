"""Fabricated, simulated v1 documents shared by focused tests."""

from reckoner.contracts import content_id

NOW = "2019-01-08T00:00:00Z"
HASH = "a" * 64
MODEL = "anthropic/claude-haiku-4-5-20251001"


def identified(document, key):
    document[key] = content_id({name: value for name, value in document.items() if name != key})
    return document


def price_fixture(model="jev-1.13.0"):
    return identified(
        {
            "schema_version": "price-table-v1",
            "model": model,
            "currency": "USD",
            "input_per_million": "0.042" if model == "jev-1.13.0" else "1.00",
            "output_per_million": "0" if model == "jev-1.13.0" else "5.00",
            "retrieved_at": "2026-09-30T00:00:00Z",
            "source_url": "https://docs.typesafe.ai/"
            if model == "jev-1.13.0"
            else "https://platform.claude.com/docs/en/about-claude/pricing",
        },
        "price_table_version",
    )


def config_fixture(tenant="tenant-a", threshold=HASH):
    return identified(
        {
            "schema_version": "reckoner-run-config-v1",
            "tenant_id": tenant,
            "workflow_version": "reckoner-v1",
            "scorer": {
                "provider": "typesafe",
                "model": "jev-1.13.0",
                "question_version": "binary-v1",
                "price_table": price_fixture(),
            },
            "note_model": {
                "provider": "anthropic",
                "model": MODEL,
                "prompt_version": "note-v1",
                "price_table": price_fixture(MODEL),
            },
            "judge_model": {
                "provider": "anthropic",
                "model": MODEL,
                "prompt_version": "judge-v1",
                "price_table": price_fixture(MODEL),
            },
            "threshold_config_id": threshold,
            "feature_version": "features-v1",
            "scaler_id": None,
            "calibration_id": None,
            "score_mode": "raw",
            "graph_version": "graph-v1",
            "retrieval_version": "retrieval-v1",
            "resolution_policy_version": "simulated-seven-days-v1",
            "limits": {
                "timeout_seconds": 30,
                "input_token_ceiling": 2000,
                "max_output_tokens": 1000,
                "maximum_attempts": 1,
            },
        },
        "config_id",
    )


def evidence_fixture(tenant="tenant-a", transaction_id="transaction-a"):
    return identified(
        {
            "schema_version": "reckoner-evidence-v1",
            "tenant_id": tenant,
            "transaction_id": transaction_id,
            "query_time": NOW,
            "source_snapshot_ids": {"postgres": HASH, "graph": None},
            "cutoffs": {"history_before": NOW, "resolved_before": NOW, "graph_before": None},
            "coverage": {"status": "available", "missing": []},
            "features": {"amount_usd": "20.00"},
            "risk_indicators": [],
            "comparable_cases": [],
        },
        "evidence_id",
    )


def score_fixture(fraud="0.2", legitimate="0.8"):
    return {
        "schema_version": "reckoner-score-v1",
        "tenant_id": "tenant-a",
        "run_id": "run-a",
        "task_id": "task-a",
        "call_id": "call-a",
        "raw_probability": fraud,
        "distribution": {"fraud": fraud, "legitimate": legitimate},
        "confidence": "0.8",
        "requested_model": "jev-1.13.0",
        "reported_model": "jev-1.13.0",
        "question_version": "binary-v1",
        "input_sha256": HASH,
        "usage": {"input_tokens": 10, "output_tokens": 0},
        "cost": {
            "status": "settled",
            "amount": "0.00000042",
            "currency": "USD",
            "price_table_id": price_fixture()["price_table_version"],
        },
        "attempt_status": "responded",
        "adjusted_probability": None,
        "calibration_id": None,
    }


def decision_fixture(config=None, evidence=None, degraded=False):
    config = config or config_fixture()
    evidence = evidence or evidence_fixture()
    return identified(
        {
            "schema_version": "reckoner-decision-v1",
            "tenant_id": config["tenant_id"],
            "run_id": "run-a",
            "task_id": "task-a",
            "transaction_id": evidence["transaction_id"],
            "outcome": "escalate",
            "config_id": config["config_id"],
            "evidence_id": evidence["evidence_id"],
            "call_id": None if degraded else "call-a",
            "raw_probability": None if degraded else "0.2",
            "effective_probability": None if degraded else "0.2",
            "effective_low_threshold": "0.05",
            "effective_high_threshold": "0.90",
            "scorer_status": "unavailable" if degraded else "succeeded",
            "degraded_reason": "scorer unavailable" if degraded else None,
            "started_at": NOW,
            "completed_at": "2019-01-08T00:00:01Z",
        },
        "decision_id",
    )


def experiment_fixture(config=None, transaction_id="transaction-a"):
    config = config or config_fixture()
    return identified(
        {
            "schema_version": "reckoner-experiment-v1",
            "tenant_id": config["tenant_id"],
            "run_id": "run-a",
            "purpose": "development",
            "config_id": config["config_id"],
            "dataset_simulated": True,
            "dataset_version": HASH,
            "cohort_version": HASH,
            "code_revision": "d3ed204",
            "created_at": NOW,
            "tasks": [{"task_id": "task-a", "transaction_id": transaction_id}],
        },
        "experiment_id",
    )


def note_fixture():
    return identified(
        {
            "schema_version": "reckoner-case-note-v1",
            "tenant_id": "tenant-a",
            "case_id": "case-a",
            "decision_id": HASH,
            "prompt_version": "note-v1",
            "requested_model": MODEL,
            "reported_model": MODEL,
            "generation_status": "succeeded",
            "evidence_id": HASH,
            "call_id": "note-call-a",
            "started_at": NOW,
            "completed_at": "2019-01-08T00:00:01Z",
            "verdict_recommendation": "approve",
            "confidence": {"value": "0.2", "meaning": "raw_choice_probability"},
            "risk_indicators": [],
            "entity_neighbourhood": {"summary": "No neighbours.", "evidence_refs": [HASH]},
            "comparable_cases": [],
            "what_would_change_verdict": [
                {"action": "Confirm card possession", "evidence_refs": [HASH]}
            ],
        },
        "note_id",
    )


def review_fixture():
    return {
        "schema_version": "reckoner-review-v1",
        "tenant_id": "tenant-a",
        "case_id": "case-a",
        "action_id": "action-a",
        "decision_id": HASH,
        "reviewer_id": "reviewer-a",
        "reviewer_type": "human",
        "verdict": "approve",
        "recommendation": "approve",
        "idempotency_key": "retry-a",
        "prior_case_version": 1,
        "reviewed_at": NOW,
        "oracle_version": None,
        "resolution_policy_version": None,
    }


def resolution_fixture():
    return identified(
        {
            "schema_version": "reckoner-resolution-v1",
            "tenant_id": "tenant-a",
            "transaction_id": "transaction-a",
            "verdict": "approve",
            "oracle_version": "oracle-v1",
            "resolved_at": NOW,
            "resolution_policy_version": "simulated-seven-days-v1",
            "provenance": {"dataset_simulated": True, "source": "simulated-label-oracle"},
        },
        "resolution_id",
    )

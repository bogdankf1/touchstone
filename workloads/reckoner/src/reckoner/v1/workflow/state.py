"""Checkpoint-safe identifiers, scalar results and bounded status categories."""

from typing import TypedDict


class WorkflowState(TypedDict, total=False):
    tenant_id: str
    run_id: str
    task_id: str
    transaction_id: str
    config_id: str
    evidence_id: str
    calibration_id: str | None
    protocol_id: str | None
    checkpoint_version: int
    started_at: str
    evidence_status: str
    pagerank_status: str
    call_id: str | None
    scorer_status: str
    cost_status: str
    raw_probability: str | None
    effective_probability: str | None
    effective_low_threshold: str
    effective_high_threshold: str
    outcome: str
    degraded_reason: str | None
    decision_id: str
    status: str

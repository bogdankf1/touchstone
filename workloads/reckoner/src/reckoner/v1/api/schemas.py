"""Request contracts for the local demo; actor names provide attribution only."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

OpaqueID = Annotated[
    str, StringConstraints(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")
]
CaseStatus = Literal["all", "open", "resolved", "note_pending", "note_failed", "degraded"]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReviewRequest(Request):
    tenant_id: OpaqueID
    case_id: OpaqueID
    reviewer_id: OpaqueID
    verdict: Literal["approve", "decline"]
    idempotency_key: OpaqueID
    expected_version: Annotated[int, Field(ge=1)]

    @field_validator("reviewer_id")
    @classmethod
    def human_actor(cls, value):
        if value.lower().startswith("simulat"):
            raise ValueError("reserved simulated identity")
        return value


class ActivationRequest(Request):
    tenant_id: OpaqueID
    config_id: OpaqueID
    expected_version: Annotated[int, Field(ge=0)]
    idempotency_key: OpaqueID


class ConfigurationRequest(Request):
    configuration: dict
    thresholds: dict
    evidence_mode: Literal["relational", "gds-augmented"]
    data_kind: Literal["fabricated", "simulated-cctd"]
    calibration: dict | None = None


class ReviewSummary(BaseModel):
    action_id: str
    case_id: str
    decision_id: str
    reviewer_id: str
    reviewer_type: Literal["human", "simulated"]
    verdict: Literal["approve", "decline"]
    recommendation: Literal["approve", "decline"] | None
    prior_case_version: int
    reviewed_at: str
    idempotency_key: str


class ReviewResponse(ReviewSummary):
    schema_version: Literal["reckoner-review-v1"]
    tenant_id: str


class CaseSummary(BaseModel):
    tenant_id: str
    case_id: str
    decision_id: str
    run_id: str
    transaction_id: str
    status: Literal["open", "resolved"]
    version: int
    created_at: datetime
    note_status: str
    degraded: bool
    amount_minor: str
    currency: str


class GraphNode(BaseModel):
    id: str
    tenant_id: str
    kind: str
    identity: str


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    kind: str


class GraphResponse(BaseModel):
    status: Literal["available", "unavailable"]
    total_nodes: int
    total_edges: int
    truncated: bool
    cross_tenant: bool
    nodes: Annotated[list[GraphNode], Field(max_length=100)]
    edges: Annotated[list[GraphEdge], Field(max_length=200)]


class CaseDetail(CaseSummary):
    task_id: str
    config_id: str
    evidence_id: str
    degraded_reason: str | None
    occurred_at: str
    raw_probability: str | None
    effective_probability: str | None
    effective_low_threshold: str
    effective_high_threshold: str
    coverage: dict
    cutoffs: dict
    source_snapshot_ids: dict
    graph: GraphResponse
    note: dict | None
    available_before_review: bool | None
    review: ReviewSummary | None


class CasePage(BaseModel):
    items: list[CaseSummary]
    next_cursor: str | None
    limit: int


class ConfigurationEntry(BaseModel):
    tenant_id: str
    config_id: str
    configuration: dict
    thresholds: dict
    qualification: dict | None


class ActivationResponse(BaseModel):
    tenant_id: str
    config_id: str | None
    version: int


class ModelEntry(BaseModel):
    provider: str
    model: str
    purpose: str
    price_table: dict


class ConfigurationHistory(BaseModel):
    items: list[ConfigurationEntry]
    activation: ActivationResponse
    models: list[ModelEntry]


class PreviewResponse(BaseModel):
    tenant_id: str
    run_id: str
    config_id: str
    estimate: Literal[True]
    status: Literal["available", "incompatible_scores"]
    counts: dict[str, int] | None
    completed_decisions: int
    missing_scores: int | None
    model_cost: None
    note_cost: Literal["not_estimated"]
    provider_calls: Literal[0]

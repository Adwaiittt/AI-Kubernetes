"""Request/response models for the HTTP API (these define the OpenAPI schema)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.report import RootCauseReport
from app.executor.schemas import PlanResult

IncidentStatusName = Literal[
    "investigating",
    "pending_approval",
    "no_action",
    "approved",
    "applying",
    "applied",
    "rejected",
    "failed",
]


class IncidentCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "namespace": "demo",
                    "target": "checkout",
                    "symptom": "checkout pods restart every few minutes; 502s at the edge",
                }
            ]
        },
    )

    namespace: str = Field(
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        max_length=63,
        description="Namespace to investigate.",
    )
    target: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$",
        max_length=253,
        description="Optional workload (usually a Deployment) the symptom is about.",
    )
    symptom: str = Field(min_length=3, max_length=4000, description="What is going wrong.")


class TokenUsageOut(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int


class IncidentSummary(BaseModel):
    id: str
    namespace: str
    target: str | None
    symptom: str
    status: IncidentStatusName
    created_at: datetime
    updated_at: datetime
    confidence: Literal["high", "medium", "low"] | None = None
    summary: str | None = None
    proposed_action: str | None = None  # action_type of the proposed fix


class TraceEvent(BaseModel):
    seq: int
    type: str
    at: datetime
    data: dict[str, Any]


class ApprovalOut(BaseModel):
    id: str
    decision: Literal["approved", "rejected"]
    actor: str
    comment: str | None
    created_at: datetime
    consumed_at: datetime | None


class IncidentDetail(IncidentSummary):
    error: str | None
    model: str | None
    report: RootCauseReport | None
    tool_calls: int
    model_turns: int
    latency_ms: int | None
    usage: TokenUsageOut
    dry_run: PlanResult | None
    execution: PlanResult | None
    rollout_status: dict[str, Any] | None
    approval: ApprovalOut | None
    trace: list[TraceEvent]


class IncidentList(BaseModel):
    items: list[IncidentSummary]
    total: int


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    comment: str | None = Field(default=None, max_length=2000)


class AuditEntry(BaseModel):
    id: int
    at: datetime
    actor: str
    action: str
    incident_id: str | None
    from_status: str | None
    to_status: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    detail: str | None


class AuditList(BaseModel):
    items: list[AuditEntry]


class Health(BaseModel):
    status: Literal["ok"]


class CheckResult(BaseModel):
    ok: bool
    detail: str | None = None


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckResult]


class ErrorBody(BaseModel):
    detail: str

"""Read-side queries for the API (no state changes here)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.models.db import Approval, AuditLog, Incident, InvestigationEvent
from app.models.schemas import (
    ApprovalOut,
    AuditEntry,
    IncidentDetail,
    IncidentSummary,
    TokenUsageOut,
    TraceEvent,
)


def _summary_fields(incident: Incident) -> dict[str, Any]:
    report = incident.report or {}
    fix = report.get("proposed_fix") or {}
    return {
        "id": incident.id,
        "namespace": incident.namespace,
        "target": incident.target,
        "symptom": incident.symptom,
        "status": incident.status,
        "created_at": incident.created_at,
        "updated_at": incident.updated_at,
        "confidence": report.get("confidence"),
        "summary": report.get("summary"),
        "proposed_action": fix.get("action_type"),
    }


def list_incidents(
    engine: Engine, *, status: str | None, limit: int, offset: int
) -> tuple[list[IncidentSummary], int]:
    with Session(engine) as session:
        stmt = select(Incident)
        count = select(func.count()).select_from(Incident)
        if status:
            stmt = stmt.where(Incident.status == status)
            count = count.where(col(Incident.status) == status)
        stmt = stmt.order_by(col(Incident.created_at).desc()).offset(offset).limit(limit)
        rows = session.exec(stmt).all()
        total = session.exec(count).one()
        return [IncidentSummary(**_summary_fields(r)) for r in rows], int(total)


def get_incident(engine: Engine, incident_id: str) -> Incident | None:
    with Session(engine) as session:
        return session.get(Incident, incident_id)


def events(engine: Engine, incident_id: str, after_seq: int = -1) -> list[TraceEvent]:
    with Session(engine) as session:
        stmt = (
            select(InvestigationEvent)
            .where(InvestigationEvent.incident_id == incident_id)
            .where(col(InvestigationEvent.seq) > after_seq)
            .order_by(col(InvestigationEvent.seq))
        )
        return [
            TraceEvent(seq=e.seq, type=e.type, at=e.at, data=e.data) for e in session.exec(stmt)
        ]


def incident_detail(engine: Engine, incident_id: str) -> IncidentDetail | None:
    with Session(engine) as session:
        incident = session.get(Incident, incident_id)
        if incident is None:
            return None
        approval = session.exec(select(Approval).where(Approval.incident_id == incident_id)).first()
    return IncidentDetail(
        **_summary_fields(incident),
        error=incident.error,
        model=incident.model,
        report=incident.report,
        tool_calls=incident.tool_calls,
        model_turns=incident.model_turns,
        latency_ms=incident.latency_ms,
        usage=TokenUsageOut(
            input_tokens=incident.input_tokens,
            output_tokens=incident.output_tokens,
            cache_read_input_tokens=incident.cache_read_input_tokens,
            cache_creation_input_tokens=incident.cache_creation_input_tokens,
        ),
        dry_run=incident.dry_run,
        execution=incident.execution,
        rollout_status=incident.rollout_status,
        approval=ApprovalOut(
            id=approval.id,
            decision=approval.decision,
            actor=approval.actor,
            comment=approval.comment,
            created_at=approval.created_at,
            consumed_at=approval.consumed_at,
        )
        if approval
        else None,
        trace=events(engine, incident_id),
    )


def audit_entries(engine: Engine, *, incident_id: str | None, limit: int) -> list[AuditEntry]:
    with Session(engine) as session:
        stmt = select(AuditLog)
        if incident_id:
            stmt = stmt.where(AuditLog.incident_id == incident_id)
        stmt = stmt.order_by(col(AuditLog.id).desc()).limit(limit)
        return [
            AuditEntry(
                id=a.id or 0,
                at=a.at,
                actor=a.actor,
                action=a.action,
                incident_id=a.incident_id,
                from_status=a.from_status,
                to_status=a.to_status,
                before=a.before,
                after=a.after,
                detail=a.detail,
            )
            for a in session.exec(stmt)
        ]

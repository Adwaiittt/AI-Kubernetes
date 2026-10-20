"""SQLModel tables and engine helpers (SQLite)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DDL, JSON, Column, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Field, SQLModel, create_engine


def _now() -> datetime:
    return datetime.now(UTC)


class IncidentStatus:
    INVESTIGATING = "investigating"
    PENDING_APPROVAL = "pending_approval"  # report has a proposed fix awaiting a human
    NO_ACTION = "no_action"  # report done, nothing to apply
    APPROVED = "approved"
    APPLYING = "applying"
    APPLIED = "applied"
    REJECTED = "rejected"
    FAILED = "failed"


class Incident(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    namespace: str = Field(index=True)
    target: str | None = None
    symptom: str
    status: str = Field(default=IncidentStatus.INVESTIGATING, index=True)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    # Investigation outcome
    model: str | None = None
    report: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    error: str | None = None
    tool_calls: int = 0
    model_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    latency_ms: int | None = None

    # Remediation
    action: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # ActionRequest
    dry_run: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # PlanResult
    execution: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # PlanResult
    rollout_status: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))


class Approval(SQLModel, table=True):
    """The human decision on an incident. At most one per incident (unique incident_id)."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    incident_id: str = Field(foreign_key="incident.id", unique=True, index=True)
    decision: str  # approved | rejected
    actor: str
    comment: str | None = None
    action_sha256: str
    created_at: datetime = Field(default_factory=_now)
    consumed_at: datetime | None = None


class AuditLog(SQLModel, table=True):
    """Append-only record of every state transition and remediation step.

    UPDATE and DELETE are rejected by SQLite triggers (see below), not just by convention.
    """

    __tablename__ = "audit_log"

    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(default_factory=_now, index=True)
    actor: str
    action: str
    incident_id: str | None = Field(default=None, index=True)
    from_status: str | None = None
    to_status: str | None = None
    before: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    after: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    detail: str | None = None


_ddl: Callable[[str], Any] = DDL  # SQLAlchemy's DDL is untyped

for _op in ("UPDATE", "DELETE"):
    event.listen(
        AuditLog.__table__,  # type: ignore[attr-defined]
        "after_create",
        _ddl(
            f"CREATE TRIGGER audit_log_no_{_op.lower()} BEFORE {_op} ON audit_log "
            "BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;"
        ).execute_if(dialect="sqlite"),
    )


class InvestigationEvent(SQLModel, table=True):
    """One entry of the agent transcript: every model turn, tool call and tool result."""

    __tablename__ = "investigation_event"

    id: int | None = Field(default=None, primary_key=True)
    incident_id: str = Field(foreign_key="incident.id", index=True)
    seq: int
    type: str
    at: datetime
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


def create_db_engine(url: str) -> Engine:
    kwargs: dict[str, Any] = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool  # one shared in-memory DB (tests)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def init_db(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)

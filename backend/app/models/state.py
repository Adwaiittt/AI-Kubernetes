"""Incident status machine. Every status change goes through :func:`transition`, which writes an
audit row in the same transaction and uses compare-and-swap so concurrent changes can't both win.

    investigating -> pending_approval | no_action | failed
    pending_approval -> approved | rejected
    approved -> applying | failed
    applying -> applied | failed
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, col, update

from app.models.db import AuditLog, Incident
from app.models.db import IncidentStatus as S

TRANSITIONS: dict[str, frozenset[str]] = {
    S.INVESTIGATING: frozenset({S.PENDING_APPROVAL, S.NO_ACTION, S.FAILED}),
    S.PENDING_APPROVAL: frozenset({S.APPROVED, S.REJECTED}),
    S.APPROVED: frozenset({S.APPLYING, S.FAILED}),
    S.APPLYING: frozenset({S.APPLIED, S.FAILED}),
}
TERMINAL = frozenset({S.NO_ACTION, S.APPLIED, S.REJECTED, S.FAILED})


class InvalidTransitionError(Exception):
    def __init__(self, incident_id: str, from_status: str, to_status: str) -> None:
        super().__init__(f"incident {incident_id}: cannot go from {from_status} to {to_status}")
        self.incident_id = incident_id
        self.from_status = from_status
        self.to_status = to_status


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in TRANSITIONS.get(from_status, frozenset())


def transition(
    session: Session,
    incident: Incident,
    to_status: str,
    *,
    actor: str,
    action: str,
    detail: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    """Move ``incident`` to ``to_status`` and audit it. The caller commits."""
    from_status = incident.status
    if not can_transition(from_status, to_status):
        raise InvalidTransitionError(incident.id, from_status, to_status)
    now = datetime.now(UTC)
    swapped = session.exec(
        update(Incident)
        .where(col(Incident.id) == incident.id, col(Incident.status) == from_status)
        .values(status=to_status, updated_at=now)
    )
    if swapped.rowcount != 1:
        # Someone else changed the status since we loaded it.
        session.rollback()
        session.refresh(incident)
        raise InvalidTransitionError(incident.id, incident.status, to_status)
    incident.status = to_status
    incident.updated_at = now
    entry = AuditLog(
        actor=actor,
        action=action,
        incident_id=incident.id,
        from_status=from_status,
        to_status=to_status,
        before=before if before is not None else {"status": from_status},
        after=after if after is not None else {"status": to_status},
        detail=detail,
    )
    session.add(entry)
    return entry


def audit(
    session: Session,
    *,
    actor: str,
    action: str,
    incident_id: str | None = None,
    detail: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    """Audit an event that doesn't change status (incident created, dry run, ...)."""
    entry = AuditLog(
        actor=actor,
        action=action,
        incident_id=incident_id,
        before=before,
        after=after,
        detail=detail,
    )
    session.add(entry)
    return entry

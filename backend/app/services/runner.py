"""Background execution of investigations and approved actions, with bounded concurrency.

Every task path ends with a terminal status and closes the incident's SSE stream, even if the
task itself crashes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.core.logging import get_logger
from app.models.db import Incident
from app.models.db import IncidentStatus as S
from app.models.state import InvalidTransitionError, transition
from app.services.approvals import ApprovalService
from app.services.events import EventBroker
from app.services.investigations import InvestigationService

log = get_logger(__name__)


class TaskRunner:
    def __init__(
        self,
        engine: Engine,
        broker: EventBroker,
        investigations: InvestigationService | None,
        approvals: ApprovalService | None,
        max_concurrent_investigations: int = 3,
    ) -> None:
        self._engine = engine
        self._broker = broker
        self._investigations = investigations
        self._approvals = approvals
        self._sem = asyncio.Semaphore(max_concurrent_investigations)
        self._tasks: set[asyncio.Task[Any]] = set()

    def start_investigation(self, incident_id: str) -> None:
        self._spawn(self._investigate(incident_id), name=f"investigate-{incident_id}")

    def start_apply(self, incident_id: str, approval_id: str) -> None:
        self._spawn(self._apply(incident_id, approval_id), name=f"apply-{incident_id}")

    async def drain(self) -> None:
        """Wait for all running tasks (tests, graceful shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def recover_interrupted(self) -> int:
        """At startup: investigations interrupted by a restart can't resume -> failed."""
        recovered = 0
        with Session(self._engine) as session:
            stuck = session.exec(select(Incident).where(col(Incident.status) == S.INVESTIGATING))
            for incident in stuck:
                incident.error = "investigation interrupted by a server restart"
                transition(
                    session,
                    incident,
                    S.FAILED,
                    actor="system",
                    action="investigation_interrupted",
                    detail=incident.error,
                )
                recovered += 1
            session.commit()
        return recovered

    # --- internals ----------------------------------------------------------------------

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _investigate(self, incident_id: str) -> None:
        assert self._investigations is not None
        try:
            async with self._sem:
                await self._investigations.run(incident_id)
        except Exception as exc:  # crash in the service itself: never leave it 'investigating'
            log.exception("investigation crashed", incident_id=incident_id)
            self._fail(
                incident_id, f"internal error: {type(exc).__name__}: {exc}", "investigation_crashed"
            )
        finally:
            self._broker.close(incident_id, self._status(incident_id))

    async def _apply(self, incident_id: str, approval_id: str) -> None:
        assert self._approvals is not None
        try:
            await self._approvals.apply(incident_id, approval_id)
        except Exception as exc:
            log.exception("apply crashed", incident_id=incident_id)
            self._fail(incident_id, f"internal error: {type(exc).__name__}: {exc}", "apply_crashed")

    def _fail(self, incident_id: str, error: str, action: str) -> None:
        with Session(self._engine) as session:
            incident = session.get(Incident, incident_id)
            if incident is None:
                return
            incident.error = error
            try:
                transition(session, incident, S.FAILED, actor="system", action=action, detail=error)
                session.commit()
            except InvalidTransitionError:
                session.rollback()

    def _status(self, incident_id: str) -> str:
        with Session(self._engine) as session:
            incident = session.get(Incident, incident_id)
            return incident.status if incident else "unknown"


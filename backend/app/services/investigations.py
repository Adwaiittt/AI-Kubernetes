"""Runs investigations and persists them: incident row, live transcript events, final outcome."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from app.agent.loop import AgentEvent, InvestigationResult, Investigator
from app.agent.prompts import IncidentPrompt
from app.models.db import Incident, IncidentStatus, InvestigationEvent
from app.models.state import audit, transition

Publisher = Callable[[str, AgentEvent], Awaitable[None]]


async def _no_publish(_incident_id: str, _event: AgentEvent) -> None:
    return None


class InvestigationService:
    def __init__(
        self, engine: Engine, investigator: Investigator, publish: Publisher = _no_publish
    ) -> None:
        self._engine = engine
        self._investigator = investigator
        self._publish = publish

    def create_incident(
        self, namespace: str, symptom: str, target: str | None = None, actor: str = "system"
    ) -> Incident:
        with Session(self._engine) as session:
            incident = Incident(namespace=namespace, symptom=symptom, target=target)
            session.add(incident)
            audit(
                session,
                actor=actor,
                action="incident_created",
                incident_id=incident.id,
                after={
                    "status": incident.status,
                    "namespace": namespace,
                    "target": target,
                    "symptom": symptom,
                },
            )
            session.commit()
            session.refresh(incident)
            return incident

    async def run(self, incident_id: str) -> InvestigationResult:
        incident = await asyncio.to_thread(self._load, incident_id)
        prompt = IncidentPrompt(
            namespace=incident.namespace, symptom=incident.symptom, target=incident.target
        )

        async def on_event(event: AgentEvent) -> None:
            await asyncio.to_thread(self._save_event, incident_id, event)
            await self._publish(incident_id, event)

        result = await self._investigator.run(prompt, on_event)
        await asyncio.to_thread(self._save_result, incident_id, result)
        return result

    def events(self, incident_id: str) -> list[InvestigationEvent]:
        with Session(self._engine) as session:
            stmt = (
                select(InvestigationEvent)
                .where(InvestigationEvent.incident_id == incident_id)
                .order_by(col(InvestigationEvent.seq))
            )
            return list(session.exec(stmt))

    # --- persistence helpers (run in a worker thread) ------------------------------------

    def _load(self, incident_id: str) -> Incident:
        with Session(self._engine) as session:
            incident = session.get(Incident, incident_id)
            if incident is None:
                raise KeyError(incident_id)
            return incident

    def _save_event(self, incident_id: str, event: AgentEvent) -> None:
        with Session(self._engine) as session:
            session.add(
                InvestigationEvent(
                    incident_id=incident_id,
                    seq=event.seq,
                    type=event.type,
                    at=event.at,
                    data=event.model_dump(mode="json")["data"],
                )
            )
            session.commit()

    def _save_result(self, incident_id: str, result: InvestigationResult) -> None:
        with Session(self._engine) as session:
            incident = session.get(Incident, incident_id)
            if incident is None:
                raise KeyError(incident_id)
            if result.status == "failed" or result.report is None:
                to_status, action = IncidentStatus.FAILED, "investigation_failed"
            elif result.report.proposed_fix is not None:
                to_status, action = IncidentStatus.PENDING_APPROVAL, "investigation_completed"
            else:
                to_status, action = IncidentStatus.NO_ACTION, "investigation_completed"
            incident.report = result.report.model_dump(mode="json") if result.report else None
            incident.error = result.error
            incident.model = result.model
            incident.tool_calls = result.tool_calls
            incident.model_turns = result.model_turns
            incident.input_tokens = result.usage.input_tokens
            incident.output_tokens = result.usage.output_tokens
            incident.cache_read_input_tokens = result.usage.cache_read_input_tokens
            incident.cache_creation_input_tokens = result.usage.cache_creation_input_tokens
            incident.latency_ms = result.latency_ms
            session.add(incident)
            transition(
                session,
                incident,
                to_status,
                actor="agent",
                action=action,
                detail=result.error,
                after={
                    "status": to_status,
                    "proposed_fix": incident.report.get("proposed_fix")
                    if incident.report
                    else None,
                    "tool_calls": result.tool_calls,
                },
            )
            session.commit()

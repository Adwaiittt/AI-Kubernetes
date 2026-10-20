"""Server-Sent Events for an incident's live agent trace.

Ordering guarantees: subscribe first, then replay what's persisted, then stream live events,
skipping any seq already sent and back-filling gaps from the DB. The stream ends with an
``end`` event once the investigation is no longer running.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.engine import Engine

from app.agent.loop import AgentEvent
from app.models.db import IncidentStatus as S
from app.models.schemas import TraceEvent
from app.services import queries
from app.services.events import EventBroker, StreamEnd


def sse(event: str, data: Any, event_id: int | None = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, default=str, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


def _trace(event: TraceEvent) -> str:
    return sse("trace", event.model_dump(mode="json"), event.seq)


async def incident_stream(
    engine: Engine,
    broker: EventBroker,
    incident_id: str,
    *,
    keepalive_seconds: float = 15.0,
    after_seq: int = -1,
) -> AsyncIterator[str]:
    queue = broker.subscribe(incident_id)
    try:
        last = after_seq
        for event in await asyncio.to_thread(queries.events, engine, incident_id, last):
            yield _trace(event)
            last = event.seq

        incident = await asyncio.to_thread(queries.get_incident, engine, incident_id)
        if incident is None or incident.status != S.INVESTIGATING:
            yield sse("end", {"status": incident.status if incident else "unknown"})
            return

        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=keepalive_seconds)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if isinstance(item, StreamEnd):
                # Flush anything persisted that we haven't sent, then finish.
                for event in await asyncio.to_thread(queries.events, engine, incident_id, last):
                    yield _trace(event)
                    last = event.seq
                yield sse("end", {"status": item.status})
                return
            assert isinstance(item, AgentEvent)
            if item.seq <= last:
                continue
            if item.seq > last + 1:  # we missed live events (slow consumer): back-fill
                for event in await asyncio.to_thread(queries.events, engine, incident_id, last):
                    if event.seq >= item.seq:
                        break
                    yield _trace(event)
                    last = event.seq
            yield _trace(
                TraceEvent(
                    seq=item.seq,
                    type=item.type,
                    at=item.at,
                    data=item.model_dump(mode="json")["data"],
                )
            )
            last = item.seq
    finally:
        broker.unsubscribe(incident_id, queue)

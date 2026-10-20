"""The SSE generator under live conditions: replay, dedupe, gap back-fill, keepalive, end."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.agent.loop import AgentEvent
from app.api.stream import incident_stream, sse
from app.models.db import Incident, InvestigationEvent, create_db_engine, init_db
from app.services.events import EventBroker


def _setup(tmp_path: Path | None = None) -> tuple[Engine, EventBroker, str]:
    # A file DB, like production: each session gets its own connection. (The in-memory
    # StaticPool shares one connection across threads, which races under concurrent use.)
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())
    engine = create_db_engine(f"sqlite:///{tmp_path / 'sse.db'}")
    init_db(engine)
    with Session(engine) as session:
        inc = Incident(namespace="demo", symptom="x")
        session.add(inc)
        session.commit()
        return engine, EventBroker(), inc.id


def _persist(engine: Engine, incident_id: str, seq: int) -> AgentEvent:
    event = AgentEvent(seq=seq, type="tool_call", data={"n": seq})
    with Session(engine) as session:
        session.add(
            InvestigationEvent(
                incident_id=incident_id, seq=seq, type=event.type, at=event.at, data=event.data
            )
        )
        session.commit()
    return event


async def _collect(gen: AsyncIterator[str], out: list[str]) -> None:
    async for chunk in gen:
        out.append(chunk)


def _ids(chunks: list[str]) -> list[str]:
    ids = []
    for chunk in chunks:
        if chunk.startswith("id: "):
            ids.append(chunk.split("\n", 1)[0][4:])
        elif chunk.startswith("event: end"):
            ids.append("end")
        elif chunk.startswith(":"):
            ids.append("keepalive")
    return ids


async def test_live_stream_replays_dedupes_backfills_and_ends(tmp_path: Path) -> None:
    engine, broker, iid = _setup(tmp_path)
    e0, e1 = _persist(engine, iid, 0), _persist(engine, iid, 1)
    out: list[str] = []
    task = asyncio.create_task(
        _collect(incident_stream(engine, broker, iid, keepalive_seconds=0.05), out)
    )
    while broker.subscriber_count(iid) == 0 or len(out) < 2:
        await asyncio.sleep(0.01)

    await broker.publish(iid, e1)  # already replayed -> skipped
    await broker.publish(iid, _persist(engine, iid, 2))
    _persist(engine, iid, 3)  # persisted but its live publish was "lost"
    await asyncio.sleep(0.12)  # long enough for a keepalive
    await broker.publish(iid, _persist(engine, iid, 4))
    _persist(engine, iid, 5)  # persisted right before the end marker
    broker.close(iid, "pending_approval")
    await asyncio.wait_for(task, timeout=2)

    ids = [i for i in _ids(out) if i != "keepalive"]
    assert ids == ["0", "1", "2", "3", "4", "5", "end"]
    assert "keepalive" in _ids(out)
    assert out[-1] == sse("end", {"status": "pending_approval"})
    assert broker.subscriber_count(iid) == 0  # unsubscribed on exit
    assert e0.seq == 0


async def test_stream_ends_immediately_for_finished_incident(tmp_path: Path) -> None:
    engine, broker, iid = _setup(tmp_path)
    with Session(engine) as session:
        inc = session.get(Incident, iid)
        assert inc is not None
        inc.status = "failed"
        session.add(inc)
        session.commit()
    _persist(engine, iid, 0)
    out: list[str] = []
    await asyncio.wait_for(_collect(incident_stream(engine, broker, iid), out), timeout=2)
    assert _ids(out) == ["0", "end"]


async def test_full_queue_still_delivers_end_marker() -> None:
    broker = EventBroker(queue_size=2)
    queue = broker.subscribe("i")
    for seq in range(5):
        await broker.publish("i", AgentEvent(seq=seq, type="nudge", at=datetime.now(UTC)))
    broker.close("i", "failed")
    items = [queue.get_nowait() for _ in range(queue.qsize())]
    assert items[-1].__class__.__name__ == "StreamEnd"
    broker.unsubscribe("i", queue)
    broker.unsubscribe("i", queue)  # idempotent
    assert broker.subscriber_count("i") == 0

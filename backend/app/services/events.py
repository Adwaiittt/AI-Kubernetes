"""In-process pub/sub of agent events, feeding the SSE endpoint.

Subscribers get a bounded queue. A slow subscriber loses live events rather than blocking the
investigation; the SSE stream fills gaps from the persisted transcript (events are stored before
they are published), so nothing is lost for good.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass

from app.agent.loop import AgentEvent


@dataclass(frozen=True)
class StreamEnd:
    """Sent once an investigation has finished and its outcome is persisted."""

    status: str


Item = AgentEvent | StreamEnd


class EventBroker:
    def __init__(self, queue_size: int = 1000) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[Item]]] = defaultdict(set)
        self._queue_size = queue_size

    def subscribe(self, incident_id: str) -> asyncio.Queue[Item]:
        queue: asyncio.Queue[Item] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers[incident_id].add(queue)
        return queue

    def unsubscribe(self, incident_id: str, queue: asyncio.Queue[Item]) -> None:
        subs = self._subscribers.get(incident_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                del self._subscribers[incident_id]

    async def publish(self, incident_id: str, event: AgentEvent) -> None:
        self._put(incident_id, event)

    def close(self, incident_id: str, status: str) -> None:
        self._put(incident_id, StreamEnd(status=status))

    def subscriber_count(self, incident_id: str) -> int:
        return len(self._subscribers.get(incident_id, ()))

    def _put(self, incident_id: str, item: Item) -> None:
        for queue in list(self._subscribers.get(incident_id, ())):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                if isinstance(item, StreamEnd):
                    # Make room: the end marker must arrive so the stream can close.
                    queue.get_nowait()
                    queue.put_nowait(item)

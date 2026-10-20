"""``get_events``: namespace events, newest first, deduplicated."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from app.agent.tools._format import iso
from app.agent.tools.base import (
    Namespace,
    ObjectName,
    Tool,
    ToolContext,
    ToolInput,
    ToolOutput,
    cap_list,
)

_EPOCH = datetime.min.replace(tzinfo=UTC)


class GetEventsInput(ToolInput):
    namespace: Namespace
    involved_object: ObjectName | None = Field(
        default=None,
        description="Only events about this object name (pod, deployment, replicaset, ...).",
    )


class EventSummary(ToolOutput):
    type: str | None  # Normal | Warning
    reason: str | None
    object: str  # "Pod/checkout-7d9f-abc"
    message: str | None
    count: int
    first_seen: str | None
    last_seen: str | None


class GetEventsOutput(ToolOutput):
    namespace: str
    events: list[EventSummary]
    omitted: int = 0


def _last_seen(ev: Any) -> datetime | None:
    series_last = ev.series.last_observed_time if getattr(ev, "series", None) else None
    ts: datetime | None = (
        series_last or ev.last_timestamp or ev.event_time or ev.metadata.creation_timestamp
    )
    return ts


def _first_seen(ev: Any) -> datetime | None:
    ts: datetime | None = ev.first_timestamp or ev.event_time or ev.metadata.creation_timestamp
    return ts


def _count(ev: Any) -> int:
    if getattr(ev, "series", None) and ev.series.count:
        return int(ev.series.count)
    return int(ev.count or 1)


def get_events(ctx: ToolContext, params: GetEventsInput) -> GetEventsOutput:
    kwargs: dict[str, Any] = {"_request_timeout": ctx.clients.request_timeout}
    if params.involved_object:
        kwargs["field_selector"] = f"involvedObject.name={params.involved_object}"
    result = ctx.clients.core.list_namespaced_event(params.namespace, **kwargs)

    # Deduplicate on (object, type, reason, message); the kubelet re-emits identical events
    # as separate objects (e.g. after a pod is recreated), which only wastes context.
    merged: dict[tuple[str, str | None, str | None, str | None], dict[str, Any]] = {}
    for ev in result.items:
        obj = ev.involved_object
        key = (f"{obj.kind}/{obj.name}", ev.type, ev.reason, ev.message)
        first, last, count = _first_seen(ev), _last_seen(ev), _count(ev)
        if key in merged:
            entry = merged[key]
            entry["count"] += count
            entry["first"] = min(filter(None, [entry["first"], first]), default=None)
            entry["last"] = max(filter(None, [entry["last"], last]), default=None)
        else:
            merged[key] = {"count": count, "first": first, "last": last}

    ordered = sorted(merged.items(), key=lambda kv: kv[1]["last"] or _EPOCH, reverse=True)
    kept, omitted = cap_list(ordered, ctx.settings.events_max_items)
    return GetEventsOutput(
        namespace=params.namespace,
        events=[
            EventSummary(
                object=obj,
                type=etype,
                reason=reason,
                message=message,
                count=data["count"],
                first_seen=iso(data["first"]),
                last_seen=iso(data["last"]),
            )
            for (obj, etype, reason, message), data in kept
        ],
        omitted=omitted,
    )


GET_EVENTS = Tool(
    name="get_events",
    description=(
        "List Kubernetes events in a namespace (optionally for one object), newest first, with "
        "duplicates merged and counted. Shows scheduling failures, image pull errors, probe "
        "failures, OOM kills, back-off restarts and rollout scaling."
    ),
    input_model=GetEventsInput,
    func=get_events,
)

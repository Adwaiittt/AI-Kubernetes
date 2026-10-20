"""Incident endpoints: start an investigation, list, inspect, and stream the live trace."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.api.deps import State
from app.api.stream import incident_stream
from app.models.schemas import (
    ErrorBody,
    IncidentCreate,
    IncidentDetail,
    IncidentList,
    IncidentStatusName,
    IncidentSummary,
)
from app.services import queries

router = APIRouter(prefix="/incidents", tags=["incidents"])

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorBody, "description": "Incident not found"}
}


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IncidentSummary,
    summary="Start an investigation",
    description=(
        "Creates an incident and starts the AI SRE investigation in the background. "
        "Follow progress with `GET /incidents/{id}/stream`."
    ),
    responses={503: {"model": ErrorBody, "description": "Agent not configured"}},
)
async def create_incident(body: IncidentCreate, state: State) -> IncidentSummary:
    if state.investigations is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "the agent is not configured")
    incident = state.investigations.create_incident(
        body.namespace, body.symptom, body.target, actor="api"
    )
    state.runner.start_investigation(incident.id)
    detail = queries.incident_detail(state.engine, incident.id)
    assert detail is not None
    return IncidentSummary(**detail.model_dump(include=set(IncidentSummary.model_fields)))


@router.get("", response_model=IncidentList, summary="List incidents")
async def list_incidents(
    state: State,
    status_filter: Annotated[IncidentStatusName | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> IncidentList:
    items, total = queries.list_incidents(
        state.engine, status=status_filter, limit=limit, offset=offset
    )
    return IncidentList(items=items, total=total)


@router.get(
    "/{incident_id}",
    response_model=IncidentDetail,
    summary="Get an incident",
    description="The root-cause report, the full tool-call trace, dry run and execution results.",
    responses=NOT_FOUND,
)
async def get_incident(incident_id: str, state: State) -> IncidentDetail:
    detail = queries.incident_detail(state.engine, incident_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
    return detail


@router.get(
    "/{incident_id}/stream",
    summary="Stream the agent trace (SSE)",
    description=(
        "Server-Sent Events. Each `trace` event carries one transcript entry (model response, "
        "tool call, tool result, ...); `id` is its sequence number, so reconnecting with "
        "`Last-Event-ID` resumes without duplicates. A final `end` event carries the status."
    ),
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}, **NOT_FOUND},
)
async def stream_incident(
    incident_id: str,
    state: State,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    if queries.get_incident(state.engine, incident_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else -1
    return StreamingResponse(
        incident_stream(
            state.engine,
            state.broker,
            incident_id,
            keepalive_seconds=state.settings.sse_keepalive_seconds,
            after_seq=after,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

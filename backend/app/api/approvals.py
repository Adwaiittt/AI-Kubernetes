"""Approval endpoints (API-key protected): dry run, approve, reject."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status

from app.api.deps import Approver, State
from app.executor.schemas import PlanResult
from app.models.schemas import DecisionRequest, ErrorBody, IncidentDetail
from app.services import queries
from app.services.approvals import ApprovalError, ApprovalService

router = APIRouter(prefix="/incidents/{incident_id}", tags=["approvals"])

_CODES = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "conflict": status.HTTP_409_CONFLICT,
    "rejected": 422,
}
ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorBody, "description": "Missing or invalid API key"},
    404: {"model": ErrorBody, "description": "Incident not found"},
    409: {"model": ErrorBody, "description": "Incident is not in a state that allows this"},
    422: {"model": ErrorBody, "description": "The executor refused the action"},
    503: {"model": ErrorBody, "description": "Approval gate not configured"},
}


def _service(state: State) -> ApprovalService:
    if state.approvals is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "approvals are not configured")
    return state.approvals


def _http(exc: ApprovalError) -> HTTPException:
    return HTTPException(_CODES.get(exc.code, status.HTTP_400_BAD_REQUEST), exc.detail)


def _detail(state: State, incident_id: str) -> IncidentDetail:
    detail = queries.incident_detail(state.engine, incident_id)
    if detail is None:  # pragma: no cover - checked by the service first
        raise HTTPException(status.HTTP_404_NOT_FOUND, "incident not found")
    return detail


@router.post(
    "/dry-run",
    response_model=PlanResult,
    summary="Dry-run the proposed fix",
    description=(
        "Runs the proposed action as a server-side dry run through the executor and returns "
        "the exact patch and the resulting diff. Required before approval."
    ),
    responses=ERRORS,
)
async def dry_run_fix(incident_id: str, state: State, approver: Approver) -> PlanResult:
    try:
        return await _service(state).dry_run(incident_id, actor=approver)
    except ApprovalError as exc:
        raise _http(exc) from exc


@router.post(
    "/approve",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IncidentDetail,
    summary="Approve the proposed fix",
    description=(
        "Records the approval (who, when, comment) and hands the exact dry-run action to the "
        "executor in the background. The incident moves approved -> applying -> applied/failed."
    ),
    responses=ERRORS,
)
async def approve_fix(
    incident_id: str, state: State, approver: Approver, body: DecisionRequest | None = None
) -> IncidentDetail:
    service = _service(state)
    try:
        approval_id = service.record_approval(
            incident_id, actor=approver, comment=body.comment if body else None
        )
    except ApprovalError as exc:
        raise _http(exc) from exc
    state.runner.start_apply(incident_id, approval_id)
    return _detail(state, incident_id)


@router.post(
    "/reject",
    response_model=IncidentDetail,
    summary="Reject the proposed fix",
    responses=ERRORS,
)
async def reject_fix(
    incident_id: str, state: State, approver: Approver, body: DecisionRequest | None = None
) -> IncidentDetail:
    try:
        _service(state).reject(incident_id, actor=approver, comment=body.comment if body else None)
    except ApprovalError as exc:
        raise _http(exc) from exc
    return _detail(state, incident_id)

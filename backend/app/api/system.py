"""Audit log and health endpoints."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Response, status
from sqlmodel import Session, text

from app.api.deps import State
from app.models.schemas import AuditList, CheckResult, Health, Readiness
from app.services import queries

audit_router = APIRouter(tags=["audit"])
health_router = APIRouter(tags=["health"])


@audit_router.get(
    "/audit",
    response_model=AuditList,
    summary="Read the audit log",
    description="Append-only log of every state transition and remediation step, newest first.",
)
async def list_audit(
    state: State,
    incident_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> AuditList:
    return AuditList(
        items=queries.audit_entries(state.engine, incident_id=incident_id, limit=limit)
    )


@health_router.get("/healthz", response_model=Health, summary="Liveness")
async def healthz() -> Health:
    return Health(status="ok")


@health_router.get(
    "/readyz",
    response_model=Readiness,
    summary="Readiness",
    description="Checks the database, Kubernetes API reachability (reader identity), the agent "
    "configuration and the approval gate. Returns 503 if a required check fails.",
    responses={503: {"model": Readiness}},
)
async def readyz(state: State, response: Response) -> Readiness:
    checks: dict[str, CheckResult] = {}

    def db() -> None:
        with Session(state.engine) as session:
            session.exec(text("SELECT 1"))  # type: ignore[call-overload]

    try:
        await asyncio.to_thread(db)
        checks["database"] = CheckResult(ok=True)
    except Exception as exc:
        checks["database"] = CheckResult(ok=False, detail=f"{type(exc).__name__}: {exc}")

    if state.reader is None:
        checks["kubernetes"] = CheckResult(ok=False, detail="reader identity not configured")
    else:
        reader = state.reader
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    reader.clients.core.get_api_resources,
                    _request_timeout=reader.clients.request_timeout,
                ),
                timeout=reader.clients.request_timeout + 1,
            )
            checks["kubernetes"] = CheckResult(ok=True)
        except Exception as exc:
            checks["kubernetes"] = CheckResult(ok=False, detail=f"{type(exc).__name__}: {exc}")

    checks["agent"] = CheckResult(
        ok=state.investigations is not None,
        detail=None if state.investigations else "ANTHROPIC_MODEL / reader not configured",
    )
    checks["approvals"] = CheckResult(
        ok=state.approvals is not None and bool(state.approvers),
        detail=None
        if state.approvals and state.approvers
        else "EXECUTOR_API_KEY / APPROVAL_SIGNING_KEY / APPROVER_API_KEYS not configured",
    )

    # The API can serve reads without the agent or approvals; DB and cluster access are required.
    ready = checks["database"].ok and checks["kubernetes"].ok
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Readiness(status="ready" if ready else "not_ready", checks=checks)

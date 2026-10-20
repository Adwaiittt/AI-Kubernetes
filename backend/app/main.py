"""FastAPI application.

uv run uvicorn app.main:create_app --factory --port 8000
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute

from app.api import approvals, incidents, system
from app.api.deps import AppState, build_state
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger

log = get_logger(__name__)

DESCRIPTION = """\
An AI SRE agent that investigates Kubernetes incidents with **read-only** tools, writes a
root-cause report with a proposed fix, and applies the fix **only after a human approves it**.
The approved action is carried out by a separate executor running under its own RBAC identity.

Approve/reject/dry-run require an approver API key in the `X-API-Key` header.
"""

TAGS = [
    {"name": "incidents", "description": "Start investigations and inspect their results."},
    {"name": "approvals", "description": "The human approval gate (API key required)."},
    {"name": "audit", "description": "Append-only audit log."},
    {"name": "health", "description": "Liveness and readiness probes."},
]


def _operation_id(route: APIRoute) -> str:
    return route.name  # e.g. create_incident -> clean names in generated clients


def create_app(state: AppState | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or (state.settings if state else get_settings())
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app_state = state or build_state(settings)
        app.state.monitor = app_state
        recovered = app_state.runner.recover_interrupted()
        if recovered:
            log.warning("marked interrupted investigations as failed", count=recovered)
        yield
        await app_state.runner.shutdown()
        if app_state.executor is not None:
            await app_state.executor.aclose()

    app = FastAPI(
        title="Kubernetes AI Monitor",
        version="0.1.0",
        description=DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        generate_unique_id_function=_operation_id,
    )
    if state is not None:  # tests drive the app without running the lifespan
        app.state.monitor = state
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key", "Last-Event-ID"],
    )
    app.include_router(incidents.router)
    app.include_router(approvals.router)
    app.include_router(system.audit_router)
    app.include_router(system.health_router)
    return app

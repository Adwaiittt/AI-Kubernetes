"""Application wiring and FastAPI dependencies."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.engine import Engine

from app.agent.loop import AgentConfig, ConfigurationError, Investigator, build_anthropic_client
from app.agent.tools import build_registry
from app.agent.tools.base import ToolContext
from app.core.config import Settings
from app.core.k8s import build_reader_clients
from app.core.logging import get_logger
from app.executor.client import ExecutorClient
from app.models.db import create_db_engine, init_db
from app.services.approvals import ApprovalService
from app.services.events import EventBroker
from app.services.investigations import InvestigationService
from app.services.runner import TaskRunner

log = get_logger(__name__)


@dataclass
class AppState:
    settings: Settings
    engine: Engine
    broker: EventBroker
    runner: TaskRunner
    investigations: InvestigationService | None  # None: agent not configured
    approvals: ApprovalService | None  # None: approval gate not configured
    reader: ToolContext | None
    executor: ExecutorClient | None
    approvers: dict[str, str]  # api key -> approver name


def build_state(settings: Settings) -> AppState:
    """Wire real dependencies. Missing optional config disables a feature, it doesn't crash."""
    engine = create_db_engine(settings.database_url)
    init_db(engine)
    broker = EventBroker()

    reader: ToolContext | None = None
    try:
        reader = ToolContext(build_reader_clients(settings), settings)
    except Exception as exc:  # no kubeconfig / not in a cluster
        log.warning("kubernetes reader not configured", error=str(exc))

    investigations: InvestigationService | None = None
    if reader is not None:
        try:
            investigator = Investigator(
                build_anthropic_client(settings),
                build_registry(),
                reader,
                AgentConfig.from_settings(settings),
            )
            investigations = InvestigationService(engine, investigator, broker.publish)
        except ConfigurationError as exc:
            log.warning("agent not configured", error=str(exc))

    executor: ExecutorClient | None = None
    approvals: ApprovalService | None = None
    if settings.executor_api_key and settings.approval_signing_key:
        executor = ExecutorClient(
            settings.executor_url, settings.executor_api_key.get_secret_value()
        )
        approvals = ApprovalService(
            engine,
            executor,
            settings.approval_signing_key.get_secret_value().encode(),
            reader=reader,
            token_ttl_seconds=settings.approval_token_ttl_seconds,
            rollout_wait_seconds=settings.rollout_wait_seconds,
        )
    else:
        log.warning("approval gate not configured (EXECUTOR_API_KEY / APPROVAL_SIGNING_KEY)")

    runner = TaskRunner(
        engine, broker, investigations, approvals, settings.max_concurrent_investigations
    )
    return AppState(
        settings,
        engine,
        broker,
        runner,
        investigations,
        approvals,
        reader,
        executor,
        settings.approvers(),
    )


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.monitor
    return state


State = Annotated[AppState, Depends(get_state)]

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    scheme_name="ApproverKey",
    description="Approver API key. The matching approver name is recorded in the audit log.",
)


def require_approver(
    state: State, key: Annotated[str | None, Security(api_key_header)] = None
) -> str:
    if not state.approvers:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "approvals are not configured")
    match: str | None = None
    for candidate, name in state.approvers.items():  # compare against all: no early exit
        if key is not None and hmac.compare_digest(key.encode(), candidate.encode()):
            match = name
    if match is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return match


Approver = Annotated[str, Depends(require_approver)]

"""The executor: a separate service that applies an action only with a valid approval token.

    uvicorn app.executor.service:create_app --factory --port 8001

It holds the only write credentials in the system (the monitor-executor ServiceAccount) and
accepts exactly two operations: a server-side dry run, and an approved execution.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.executor.actions import ActionRunner, ExecutionError, ExecutorApi
from app.executor.config import ExecutorSettings
from app.executor.schemas import (
    ActionRequest,
    DryRunRequest,
    ExecuteRequest,
    ExecutorError,
    PlanResult,
)
from app.executor.tokens import NonceStore, TokenError, verify_token
from app.executor.validation import ActionRejectedError, validate_action


class Executor:
    def __init__(self, runner: ActionRunner, nonces: NonceStore, signing_key: bytes) -> None:
        if len(signing_key) < 32:
            raise ValueError("APPROVAL_SIGNING_KEY must be at least 32 bytes")
        self._runner = runner
        self._nonces = nonces
        self._key = signing_key

    def dry_run(self, action: ActionRequest) -> PlanResult:
        return self._runner.run(action, dry_run=True)

    def execute(self, action: ActionRequest, approval_token: str) -> PlanResult:
        # 1. The token must be genuine, unexpired and bound to exactly this action.
        claims = verify_token(approval_token, self._key, action_sha256=action.digest())
        # 2. The action must pass policy *before* the approval is spent.
        validate_action(action, self._runner.policy)
        # 3. Spend the approval (the primary key makes concurrent replays lose), then apply.
        self._nonces.consume(claims)
        return self._runner.run(action, dry_run=False)


def create_executor_app(executor: Executor, api_key: str) -> FastAPI:
    if len(api_key) < 16:
        raise ValueError("EXECUTOR_API_KEY must be at least 16 characters")
    app = FastAPI(
        title="k8s-ai-monitor executor",
        version="0.1.0",
        description="Applies human-approved remediation actions. Internal service.",
    )

    def require_key(x_executor_key: Annotated[str | None, Header()] = None) -> None:
        if x_executor_key is None or not hmac.compare_digest(x_executor_key, api_key):
            raise HTTPException(status_code=401, detail="invalid executor key")

    @app.exception_handler(ActionRejectedError)
    async def _rejected(_req: Request, exc: ActionRejectedError) -> JSONResponse:
        return _error(422, "rejected", str(exc))

    @app.exception_handler(TokenError)
    async def _token(_req: Request, exc: TokenError) -> JSONResponse:
        return _error(403, "unauthorized", str(exc))

    @app.exception_handler(ExecutionError)
    async def _execution(_req: Request, exc: ExecutionError) -> JSONResponse:
        return _error(502, "kubernetes_error", str(exc))

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/dry-run", response_model=PlanResult, dependencies=[Depends(require_key)])
    def dry_run(body: DryRunRequest) -> PlanResult:
        return executor.dry_run(body.action)

    @app.post("/v1/execute", response_model=PlanResult, dependencies=[Depends(require_key)])
    def execute(body: ExecuteRequest) -> PlanResult:
        return executor.execute(body.action, body.approval_token)

    return app


def _error(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status, content=ExecutorError(code=code, detail=detail).model_dump()
    )


def create_app() -> FastAPI:  # pragma: no cover - wiring, exercised by deployment
    from kubernetes import client

    from app.core.k8s import build_api_client
    from app.models.db import create_db_engine

    settings = ExecutorSettings()
    if settings.api_key is None or settings.approval_signing_key is None:
        raise RuntimeError("EXECUTOR_API_KEY and APPROVAL_SIGNING_KEY must be set")
    api_client = build_api_client(
        str(settings.kubeconfig) if settings.kubeconfig else None, settings.kube_context
    )
    runner = ActionRunner(
        ExecutorApi(client.AppsV1Api(api_client)),
        settings.policy(),
        request_timeout=settings.request_timeout_seconds,
    )
    nonces = NonceStore(create_db_engine(settings.database_url))
    executor = Executor(runner, nonces, settings.approval_signing_key.get_secret_value().encode())
    return create_executor_app(executor, settings.api_key.get_secret_value())

"""Backend-side HTTP client for the executor service."""

from __future__ import annotations

from typing import Any

import httpx

from app.executor.schemas import ActionRequest, PlanResult


class ExecutorClientError(RuntimeError):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(f"executor {status} {code}: {detail}")
        self.status = status
        self.code = code
        self.detail = detail


class ExecutorClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-Executor-Key": api_key},
            timeout=timeout,
            transport=transport,
        )

    async def dry_run(self, action: ActionRequest) -> PlanResult:
        return await self._post("/v1/dry-run", {"action": action.model_dump(mode="json")})

    async def execute(self, action: ActionRequest, approval_token: str) -> PlanResult:
        return await self._post(
            "/v1/execute",
            {"action": action.model_dump(mode="json"), "approval_token": approval_token},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> PlanResult:
        try:
            response = await self._http.post(path, json=body)
        except httpx.HTTPError as exc:
            raise ExecutorClientError(0, "unreachable", str(exc) or type(exc).__name__) from exc
        if response.status_code != 200:
            try:
                payload = response.json()
                code = payload.get("code", "error")
                detail = payload.get("detail", response.text)
            except ValueError:
                code, detail = "error", response.text
            raise ExecutorClientError(response.status_code, str(code), str(detail))
        return PlanResult.model_validate(response.json())

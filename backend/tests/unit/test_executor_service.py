"""The executor's HTTP surface, exercised through the real backend-side client."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app.executor.actions import ActionRunner, ExecutorApi
from app.executor.client import ExecutorClient, ExecutorClientError
from app.executor.schemas import ActionRequest
from app.executor.service import Executor, create_executor_app
from app.executor.tokens import NonceStore, issue_token, new_claims
from app.executor.validation import ExecutorPolicy
from app.models.db import create_db_engine
from tests.unit.executor_fakes import EXECUTOR_KEY, SIGNING_KEY, FakeApps

SCALE = ActionRequest(
    action_type="scale", namespace="demo", target="checkout", parameters={"replicas": 3}
)


class Harness:
    def __init__(self) -> None:
        self.apps = FakeApps()
        self.engine = create_db_engine("sqlite://")
        self.nonces = NonceStore(self.engine)
        runner = ActionRunner(ExecutorApi(self.apps), ExecutorPolicy(frozenset({"demo"})))
        self.app = create_executor_app(Executor(runner, self.nonces, SIGNING_KEY), EXECUTOR_KEY)

    def client(self, key: str = EXECUTOR_KEY) -> ExecutorClient:
        return ExecutorClient("http://executor", key, transport=httpx.ASGITransport(app=self.app))

    def token(
        self,
        action: ActionRequest = SCALE,
        approval_id: str = "ap1",
        key: bytes = SIGNING_KEY,
        now: float | None = None,
    ) -> str:
        claims = new_claims(
            approval_id=approval_id,
            incident_id="inc1",
            action_sha256=action.digest(),
            approver="alice",
            ttl_seconds=300,
            now=now,
        )
        return issue_token(claims, key)


@pytest.fixture
def h() -> Iterator[Harness]:
    harness = Harness()
    yield harness
    harness.engine.dispose()


def call(coro: Any) -> Any:
    return asyncio.run(coro)


def test_requires_executor_key(h: Harness) -> None:
    with pytest.raises(ExecutorClientError) as err:
        call(h.client("wrong-key-wrong-key").dry_run(SCALE))
    assert err.value.status == 401


def test_dry_run_needs_no_approval_and_changes_nothing(h: Harness) -> None:
    result = call(h.client().dry_run(SCALE))
    assert result.dry_run is True
    assert h.apps.applied == []
    assert "+replicas: 3" in result.diff


def test_execute_with_valid_approval(h: Harness) -> None:
    result = call(h.client().execute(SCALE, h.token()))
    assert result.dry_run is False
    assert [p["body"] for p in h.apps.applied] == [{"spec": {"replicas": 3}}]
    assert h.nonces.is_consumed("ap1")


def test_action_cannot_run_without_approval(h: Harness) -> None:
    async def raw() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=h.app), base_url="http://executor"
        ) as c:
            return await c.post(
                "/v1/execute",
                json={"action": SCALE.model_dump()},
                headers={"X-Executor-Key": EXECUTOR_KEY},
            )

    assert call(raw()).status_code == 422  # approval_token is required
    for bogus in ["", "forged.token", h.token(key=b"z" * 32)]:
        with pytest.raises(ExecutorClientError):
            call(h.client().execute(SCALE, bogus))
    assert h.apps.patches == []


def test_approval_cannot_be_reused(h: Harness) -> None:
    token = h.token()
    call(h.client().execute(SCALE, token))
    with pytest.raises(ExecutorClientError) as err:
        call(h.client().execute(SCALE, token))
    assert err.value.status == 403
    assert "already been used" in err.value.detail
    assert len(h.apps.applied) == 1


def test_approval_is_bound_to_the_exact_action(h: Harness) -> None:
    bigger = SCALE.model_copy(update={"parameters": {"replicas": 10}})
    with pytest.raises(ExecutorClientError) as err:
        call(h.client().execute(bigger, h.token(SCALE)))
    assert err.value.status == 403 and "different action" in err.value.detail
    assert h.apps.applied == []


def test_expired_approval(h: Harness) -> None:
    with pytest.raises(ExecutorClientError) as err:
        call(h.client().execute(SCALE, h.token(now=1_000_000.0)))
    assert "expired" in err.value.detail


def test_non_allowlisted_namespace_refused_without_spending_approval(h: Harness) -> None:
    other = ActionRequest(
        action_type="scale", namespace="kube-system", target="coredns", parameters={"replicas": 0}
    )
    for op in (h.client().dry_run(other), h.client().execute(other, h.token(other))):
        with pytest.raises(ExecutorClientError) as err:
            call(op)
        assert err.value.status == 422 and err.value.code == "rejected"
    assert not h.nonces.is_consumed("ap1")
    assert h.apps.patches == []


def test_invalid_parameters_refused(h: Harness) -> None:
    bad = ActionRequest(
        action_type="patch_resources",
        namespace="demo",
        target="checkout",
        parameters={"container": "app", "memory_limit": "999Ti"},
    )
    with pytest.raises(ExecutorClientError) as err:
        call(h.client().execute(bad, h.token(bad)))
    assert err.value.code == "rejected" and "exceeds" in err.value.detail


def test_kubernetes_failure_maps_to_502(h: Harness) -> None:
    from tests.unit import factories as f

    h.apps.fail_patch = f.api_error(422, "spec.replicas: Invalid value")
    with pytest.raises(ExecutorClientError) as err:
        call(h.client().dry_run(SCALE))
    assert err.value.status == 502 and err.value.code == "kubernetes_error"


def test_healthz_and_unreachable(h: Harness) -> None:
    async def health() -> int:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=h.app), base_url="http://executor"
        ) as c:
            return (await c.get("/healthz")).status_code

    assert call(health()) == 200
    dead = ExecutorClient("http://127.0.0.1:9", EXECUTOR_KEY, timeout=0.5)
    with pytest.raises(ExecutorClientError) as err:
        call(dead.dry_run(SCALE))
    assert err.value.code == "unreachable"


def test_weak_secrets_rejected(h: Harness) -> None:
    runner = ActionRunner(ExecutorApi(h.apps), ExecutorPolicy(frozenset({"demo"})))
    with pytest.raises(ValueError, match="32 bytes"):
        Executor(runner, h.nonces, b"short")
    with pytest.raises(ValueError, match="16 characters"):
        create_executor_app(Executor(runner, h.nonces, SIGNING_KEY), "short")

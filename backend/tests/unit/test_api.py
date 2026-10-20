"""HTTP API tests: real app, real executor service (ASGI), scripted Claude, fake cluster."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaMessage
from kubernetes import client as k
from sqlmodel import Session

from app.agent.loop import AgentConfig, Investigator
from app.agent.report import SUBMIT_REPORT
from app.agent.tools import build_registry
from app.api.deps import AppState
from app.core.config import Settings
from app.executor.client import ExecutorClient
from app.main import create_app
from app.models.db import Incident, create_db_engine, init_db
from app.services.approvals import ApprovalService
from app.services.events import EventBroker
from app.services.investigations import InvestigationService
from app.services.runner import TaskRunner
from tests.conftest import FakeCluster
from tests.unit import factories as f
from tests.unit.agent_fakes import MODEL, fake_client, message, report_input, tool_use
from tests.unit.executor_fakes import EXECUTOR_KEY, SIGNING_KEY
from tests.unit.test_executor_service import Harness

ALICE = "alice-key-0123456789"
BOB = "bob-key-0123456789ab"
AUTH = {"X-API-Key": ALICE}


def investigation(namespace: str = "demo") -> list[BetaMessage]:
    return [
        message(tool_use("list_pods", {"namespace": namespace})),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",), namespace=namespace))),
    ]


class Api:
    def __init__(
        self,
        cluster: FakeCluster,
        *responses: BetaMessage | Exception,
        agent: bool = True,
        approvals: bool = True,
    ) -> None:
        cluster.core.list_namespaced_pod.return_value = f.pod_list(
            f.pod("checkout-7d9f-abc", statuses=[f.oom_status()])
        )
        cluster.core.get_api_resources.return_value = k.V1APIResourceList(
            group_version="v1", resources=[]
        )
        self.cluster = cluster
        self.engine = create_db_engine("sqlite://")
        init_db(self.engine)
        broker = EventBroker()
        investigations = None
        if agent:
            fake, self.claude = fake_client(*responses)
            inv = Investigator(fake, build_registry(), cluster.ctx, AgentConfig(model=MODEL))
            investigations = InvestigationService(self.engine, inv, broker.publish)
        self.executor = Harness()
        approval_svc = None
        if approvals:
            ex_client = ExecutorClient(
                "http://executor",
                EXECUTOR_KEY,
                transport=httpx.ASGITransport(app=self.executor.app),
            )
            approval_svc = ApprovalService(
                self.engine, ex_client, SIGNING_KEY, rollout_wait_seconds=0
            )
        self.state = AppState(
            settings=Settings(_env_file=None),
            engine=self.engine,
            broker=broker,
            runner=TaskRunner(self.engine, broker, investigations, approval_svc),
            investigations=investigations,
            approvals=approval_svc,
            reader=cluster.ctx,
            executor=None,
            approvers={ALICE: "alice", BOB: "bob"} if approvals else {},
        )
        self.app = create_app(self.state)
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://api"
        )

    async def investigate(self, **body: Any) -> str:
        payload = {"namespace": "demo", "target": "checkout", "symptom": "pods restarting"}
        r = await self.http.post("/incidents", json={**payload, **body})
        assert r.status_code == 202, r.text
        await self.state.runner.drain()
        incident_id: str = r.json()["id"]
        return incident_id


@pytest.fixture
async def api(cluster: FakeCluster) -> AsyncIterator[Api]:
    a = Api(cluster, *investigation())
    yield a
    await a.http.aclose()


def parse_sse(body: str) -> list[dict[str, Any]]:
    out = []
    for block in body.strip().split("\n\n"):
        fields: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            fields[key] = json.loads(value) if key == "data" else value
        if fields:
            out.append(fields)
    return out


# --- health ---------------------------------------------------------------------------------


async def test_healthz(api: Api) -> None:
    r = await api.http.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


async def test_readyz(api: Api) -> None:
    r = await api.http.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert {name: c["ok"] for name, c in body["checks"].items()} == {
        "database": True,
        "kubernetes": True,
        "agent": True,
        "approvals": True,
    }


async def test_readyz_fails_when_cluster_unreachable(api: Api) -> None:
    api.cluster.core.get_api_resources.side_effect = ConnectionError("connection refused")
    r = await api.http.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["kubernetes"] == {
        "ok": False,
        "detail": "ConnectionError: connection refused",
    }


async def test_readyz_reports_missing_features(cluster: FakeCluster) -> None:
    a = Api(cluster, agent=False, approvals=False)
    a.state.reader = None
    r = await a.http.get("/readyz")
    assert r.status_code == 503
    checks = r.json()["checks"]
    assert not checks["kubernetes"]["ok"] and not checks["agent"]["ok"]
    assert not checks["approvals"]["ok"]


# --- incidents ------------------------------------------------------------------------------


async def test_create_and_complete_investigation(api: Api) -> None:
    r = await api.http.post(
        "/incidents", json={"namespace": "demo", "target": "checkout", "symptom": "restarting"}
    )
    assert r.status_code == 202
    created = r.json()
    assert created["status"] == "investigating"
    await api.state.runner.drain()

    r = await api.http.get(f"/incidents/{created['id']}")
    assert r.status_code == 200
    detail = r.json()
    assert detail["status"] == "pending_approval"
    assert detail["report"]["confidence"] == "high"
    assert detail["report"]["proposed_fix"]["action_type"] == "patch_resources"
    assert detail["tool_calls"] == 1 and detail["model"] == MODEL
    assert detail["usage"]["input_tokens"] == 2000
    types = [e["type"] for e in detail["trace"]]
    assert types[0] == "investigation_started" and types[-1] == "report_accepted"
    tool_result = next(e for e in detail["trace"] if e["type"] == "tool_result")
    assert "checkout-7d9f-abc" in tool_result["data"]["content"]

    listing = (await api.http.get("/incidents")).json()
    assert listing["total"] == 1
    assert listing["items"][0]["proposed_action"] == "patch_resources"
    assert listing["items"][0]["summary"].startswith("checkout pods")


@pytest.mark.parametrize(
    "body",
    [
        {"namespace": "Not_Valid", "symptom": "broken"},
        {"namespace": "demo", "symptom": "x"},
        {"namespace": "demo", "symptom": "broken", "target": "Bad Name"},
        {"namespace": "demo", "symptom": "broken", "extra": True},
        {"symptom": "broken"},
    ],
)
async def test_create_validation(api: Api, body: dict[str, Any]) -> None:
    assert (await api.http.post("/incidents", json=body)).status_code == 422


async def test_create_without_agent_is_503(cluster: FakeCluster) -> None:
    a = Api(cluster, agent=False)
    r = await a.http.post("/incidents", json={"namespace": "demo", "symptom": "broken"})
    assert r.status_code == 503


async def test_unknown_incident_is_404(api: Api) -> None:
    for path in ("/incidents/nope", "/incidents/nope/stream"):
        assert (await api.http.get(path)).status_code == 404


async def test_list_filter_and_pagination(cluster: FakeCluster) -> None:
    a = Api(cluster, *investigation(), *investigation(), message(stop_reason="refusal"))
    ok1, ok2, failed = (await a.investigate(), await a.investigate(), await a.investigate())
    page = (await a.http.get("/incidents", params={"limit": 2})).json()
    assert page["total"] == 3 and len(page["items"]) == 2
    assert page["items"][0]["id"] == failed  # newest first
    only_failed = (await a.http.get("/incidents", params={"status": "failed"})).json()
    assert [i["id"] for i in only_failed["items"]] == [failed]
    pending = (await a.http.get("/incidents", params={"status": "pending_approval"})).json()
    assert {i["id"] for i in pending["items"]} == {ok1, ok2}
    assert (await a.http.get("/incidents", params={"status": "bogus"})).status_code == 422


async def test_crashed_investigation_is_marked_failed(cluster: FakeCluster) -> None:
    a = Api(cluster, RuntimeError("boom"))
    assert a.state.investigations is not None

    async def explode(_incident_id: str) -> Any:
        raise RuntimeError("database went away")

    a.state.investigations.run = explode  # type: ignore[method-assign, assignment]
    iid = await a.investigate()
    detail = (await a.http.get(f"/incidents/{iid}")).json()
    assert detail["status"] == "failed"
    assert "database went away" in detail["error"]


def test_recover_interrupted_investigations(cluster: FakeCluster) -> None:
    a = Api(cluster)
    assert a.state.investigations is not None
    stuck = a.state.investigations.create_incident("demo", "never finished")
    assert a.state.runner.recover_interrupted() == 1
    with Session(a.engine) as session:
        inc = session.get(Incident, stuck.id)
        assert inc is not None and inc.status == "failed"
        assert inc.error == "investigation interrupted by a server restart"


# --- SSE ------------------------------------------------------------------------------------


async def test_stream_replays_finished_investigation(api: Api) -> None:
    iid = await api.investigate()
    r = await api.http.get(f"/incidents/{iid}/stream")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    trace = [e for e in events if e["event"] == "trace"]
    assert [int(e["id"]) for e in trace] == list(range(len(trace)))
    assert {"tool_call", "tool_result"} <= {e["data"]["type"] for e in trace}
    call = next(e["data"] for e in trace if e["data"]["type"] == "tool_call")
    assert call["data"] == {
        "tool_use_id": call["data"]["tool_use_id"],
        "tool": "list_pods",
        "input": {"namespace": "demo"},
    }
    assert events[-1] == {"event": "end", "data": {"status": "pending_approval"}}


async def test_stream_resumes_from_last_event_id(api: Api) -> None:
    iid = await api.investigate()
    r = await api.http.get(f"/incidents/{iid}/stream", headers={"Last-Event-ID": "3"})
    trace = [e for e in parse_sse(r.text) if e["event"] == "trace"]
    assert int(trace[0]["id"]) == 4


# --- approvals ------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["dry-run", "approve", "reject"])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong-key-wrong-key"}, {"X-API-Key": ""}])
async def test_approval_endpoints_require_api_key(
    api: Api, path: str, headers: dict[str, str]
) -> None:
    iid = await api.investigate()
    r = await api.http.post(f"/incidents/{iid}/{path}", headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "ApiKey"
    detail = (await api.http.get(f"/incidents/{iid}")).json()
    assert detail["status"] == "pending_approval" and detail["approval"] is None


async def test_full_approval_flow(api: Api) -> None:
    iid = await api.investigate()

    r = await api.http.post(f"/incidents/{iid}/dry-run", headers=AUTH)
    assert r.status_code == 200
    plan = r.json()
    assert plan["dry_run"] is True and "+          memory: 512Mi" in plan["diff"]

    r = await api.http.post(
        f"/incidents/{iid}/approve", headers=AUTH, json={"comment": "limit is clearly too low"}
    )
    assert r.status_code == 202
    assert r.json()["status"] in ("approved", "applying", "applied")
    await api.state.runner.drain()

    detail = (await api.http.get(f"/incidents/{iid}")).json()
    assert detail["status"] == "applied"
    assert detail["approval"]["actor"] == "alice"
    assert detail["approval"]["comment"] == "limit is clearly too low"
    assert detail["approval"]["consumed_at"] is not None
    assert detail["execution"]["dry_run"] is False
    assert detail["dry_run"]["diff"] == plan["diff"]
    assert len(api.executor.apps.applied) == 1

    audit = (await api.http.get("/audit", params={"incident_id": iid})).json()["items"]
    actions = [(a["action"], a["actor"]) for a in reversed(audit)]
    assert actions == [
        ("incident_created", "api"), ("investigation_completed", "agent"),
        ("dry_run", "alice"), ("approved", "alice"),
        ("apply_started", "executor"), ("applied", "executor"),
    ]  # fmt: skip

    # Approving again is a conflict, not a second execution.
    assert (await api.http.post(f"/incidents/{iid}/approve", headers=AUTH)).status_code == 409
    assert len(api.executor.apps.applied) == 1


async def test_approve_requires_dry_run(api: Api) -> None:
    iid = await api.investigate()
    r = await api.http.post(f"/incidents/{iid}/approve", headers=AUTH)
    assert r.status_code == 409
    assert r.json()["detail"] == "run a dry run before approving"


async def test_reject_then_approve_is_refused(api: Api) -> None:
    iid = await api.investigate()
    await api.http.post(f"/incidents/{iid}/dry-run", headers=AUTH)
    r = await api.http.post(
        f"/incidents/{iid}/reject", headers={"X-API-Key": BOB}, json={"comment": "needs a code fix"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert r.json()["approval"] == {
        **r.json()["approval"],
        "decision": "rejected",
        "actor": "bob",
        "comment": "needs a code fix",
    }
    assert (await api.http.post(f"/incidents/{iid}/approve", headers=AUTH)).status_code == 409
    await api.state.runner.drain()
    assert api.executor.apps.applied == []


async def test_dry_run_refused_by_executor_is_422(cluster: FakeCluster) -> None:
    a = Api(cluster, *investigation(namespace="payments"))
    iid = await a.investigate(namespace="payments")
    r = await a.http.post(f"/incidents/{iid}/dry-run", headers=AUTH)
    assert r.status_code == 422
    assert "not in the executor allowlist" in r.json()["detail"]


async def test_approval_actions_on_unknown_incident(api: Api) -> None:
    for path in ("dry-run", "approve", "reject"):
        r = await api.http.post(f"/incidents/nope/{path}", headers=AUTH)
        assert r.status_code == 404


async def test_approvals_not_configured(cluster: FakeCluster) -> None:
    a = Api(cluster, *investigation(), approvals=False)
    iid = await a.investigate()
    r = await a.http.post(f"/incidents/{iid}/approve", headers=AUTH)
    assert r.status_code == 503


async def test_audit_lists_newest_first(api: Api) -> None:
    await api.investigate()
    items = (await api.http.get("/audit")).json()["items"]
    assert [i["action"] for i in items] == ["investigation_completed", "incident_created"]
    assert (await api.http.get("/audit", params={"limit": 0})).status_code == 422


# --- OpenAPI / CORS / config ----------------------------------------------------------------


async def test_openapi_is_clean(api: Api) -> None:
    schema = (await api.http.get("/openapi.json")).json()
    ops = {(m, p): o for p, item in schema["paths"].items() for m, o in item.items()}
    assert {o["operationId"] for o in ops.values()} == {
        "create_incident",
        "list_incidents",
        "get_incident",
        "stream_incident",
        "dry_run_fix",
        "approve_fix",
        "reject_fix",
        "list_audit",
        "healthz",
        "readyz",
    }
    secured = {p for (m, p), o in ops.items() if o.get("security")}
    assert secured == {
        "/incidents/{incident_id}/dry-run",
        "/incidents/{incident_id}/approve",
        "/incidents/{incident_id}/reject",
    }
    assert schema["components"]["securitySchemes"]["ApproverKey"]["in"] == "header"
    assert all(o.get("summary") and o.get("tags") for o in ops.values())
    assert "RootCauseReport" in schema["components"]["schemas"]


async def test_cors_allows_dashboard(api: Api) -> None:
    r = await api.http.options(
        "/incidents",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-API-Key",
        },
    )
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
    r = await api.http.options(
        "/incidents",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in r.headers


def test_approver_keys_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPROVER_API_KEYS", f"alice:{ALICE}, bob:{BOB}")
    assert Settings(_env_file=None).approvers() == {ALICE: "alice", BOB: "bob"}
    for bad in ("alice", "alice:short", ":nobodykey012345678"):
        monkeypatch.setenv("APPROVER_API_KEYS", bad)
        with pytest.raises(ValueError):
            Settings(_env_file=None).approvers()

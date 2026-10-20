from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from anthropic.types.beta import BetaMessage
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.agent.loop import AgentConfig, AgentEvent, Investigator
from app.agent.report import SUBMIT_REPORT
from app.agent.tools import build_registry
from app.models.db import Incident, IncidentStatus, create_db_engine, init_db
from app.services.investigations import InvestigationService
from tests.conftest import FakeCluster
from tests.unit import factories as f
from tests.unit.agent_fakes import MODEL, fake_client, message, report_input, tool_use


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_db_engine("sqlite://")
    init_db(eng)
    yield eng
    eng.dispose()


def _service(
    engine: Engine,
    cluster: FakeCluster,
    *responses: BetaMessage | Exception,
    published: list[tuple[str, AgentEvent]] | None = None,
) -> InvestigationService:
    client, _ = fake_client(*responses)
    inv = Investigator(client, build_registry(), cluster.ctx, AgentConfig(model=MODEL))

    async def publish(incident_id: str, event: AgentEvent) -> None:
        if published is not None:
            published.append((incident_id, event))

    return InvestigationService(engine, inv, publish)


def test_investigation_is_persisted(engine: Engine, cluster: FakeCluster) -> None:
    cluster.core.list_namespaced_pod.return_value = f.pod_list(f.pod("checkout-1"))
    published: list[tuple[str, AgentEvent]] = []
    svc = _service(
        engine,
        cluster,
        message(tool_use("list_pods", {"namespace": "demo"}), input_tokens=900, cache_creation=50),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",))), cache_read=950),
        published=published,
    )
    incident = svc.create_incident("demo", "checkout restarting", target="checkout")
    assert incident.status == IncidentStatus.INVESTIGATING

    result = asyncio.run(svc.run(incident.id))
    assert result.status == "completed"

    with Session(engine) as session:
        stored = session.get(Incident, incident.id)
        assert stored is not None
        assert stored.status == IncidentStatus.PENDING_APPROVAL
        assert stored.report is not None
        assert stored.report["proposed_fix"]["action_type"] == "patch_resources"
        assert stored.model == MODEL
        assert stored.tool_calls == 1
        assert stored.model_turns == 2
        assert stored.input_tokens == 1900
        assert stored.cache_read_input_tokens == 950
        assert stored.cache_creation_input_tokens == 50
        assert stored.latency_ms is not None and stored.latency_ms >= 0

    events = svc.events(incident.id)
    assert [e.seq for e in events] == list(range(len(result.transcript)))
    tool_result = next(e for e in events if e.type == "tool_result")
    assert tool_result.data["tool"] == "list_pods"
    assert "checkout-1" in tool_result.data["content"]  # full tool output is kept
    assert [e.type for _, e in published] == [e.type for e in result.transcript]
    assert {iid for iid, _ in published} == {incident.id}


def test_report_without_fix_is_no_action(engine: Engine, cluster: FakeCluster) -> None:
    cluster.core.list_namespaced_pod.return_value = f.pod_list(f.pod("checkout-1"))
    svc = _service(
        engine,
        cluster,
        message(tool_use("list_pods", {"namespace": "demo"})),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",), fix=False))),
    )
    incident = svc.create_incident("demo", "slow")
    asyncio.run(svc.run(incident.id))
    with Session(engine) as session:
        stored = session.get(Incident, incident.id)
        assert stored is not None and stored.status == IncidentStatus.NO_ACTION


def test_failed_investigation_is_recorded(engine: Engine, cluster: FakeCluster) -> None:
    svc = _service(engine, cluster, message(stop_reason="refusal"))
    incident = svc.create_incident("demo", "x")
    asyncio.run(svc.run(incident.id))
    with Session(engine) as session:
        stored = session.get(Incident, incident.id)
        assert stored is not None
        assert stored.status == IncidentStatus.FAILED
        assert stored.error is not None and "declined" in stored.error
        assert stored.report is None
    assert svc.events(incident.id)[-1].type == "investigation_failed"


def test_unknown_incident(engine: Engine, cluster: FakeCluster) -> None:
    svc = _service(engine, cluster)
    with pytest.raises(KeyError):
        asyncio.run(svc.run("nope"))

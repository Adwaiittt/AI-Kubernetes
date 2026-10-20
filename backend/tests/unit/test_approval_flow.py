"""End-to-end approval gate: backend ApprovalService -> HTTP -> real executor app -> fake k8s."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from kubernetes import client as k
from sqlalchemy.exc import DatabaseError
from sqlmodel import Session, col, select, text

from app.executor.client import ExecutorClient, ExecutorClientError
from app.executor.schemas import ActionRequest, PlanResult
from app.models.db import AuditLog, Incident, create_db_engine, init_db
from app.models.db import IncidentStatus as S
from app.models.state import InvalidTransitionError, transition
from app.services.approvals import ApprovalError, ApprovalService
from tests.conftest import FakeCluster
from tests.unit import factories as f
from tests.unit.executor_fakes import SIGNING_KEY
from tests.unit.test_executor_service import Harness

PATCH_FIX = {
    "action_type": "patch_resources",
    "namespace": "demo",
    "target": "checkout",
    "parameters": {"container": "app", "memory_limit": "512Mi"},
    "rationale": "OOMKilled at 128Mi",
    "risk": "more memory per pod",
}


class SpyClient(ExecutorClient):
    """Records tokens so tests can try replaying them."""

    tokens: list[str]

    async def execute(self, action: ActionRequest, approval_token: str) -> PlanResult:
        self.tokens.append(approval_token)
        return await super().execute(action, approval_token)


class Flow:
    def __init__(self, reader: Any = None) -> None:
        self.executor = Harness()
        self.engine = create_db_engine("sqlite://")
        init_db(self.engine)
        import httpx

        self.client = SpyClient(
            "http://executor",
            "executor-test-key-0123",
            transport=httpx.ASGITransport(app=self.executor.app),
        )
        self.client.tokens = []
        self.svc = ApprovalService(
            self.engine,
            self.client,
            SIGNING_KEY,
            reader=reader,
            rollout_wait_seconds=0,
            rollout_poll_seconds=0,
        )

    def incident(self, fix: dict[str, Any] | None = PATCH_FIX, namespace: str = "demo") -> str:
        with Session(self.engine) as session:
            inc = Incident(namespace=namespace, symptom="OOMKilled", target="checkout")
            session.add(inc)
            session.commit()
            inc.report = {
                "summary": "s",
                "root_cause": "r",
                "confidence": "high",
                "evidence": [],
                "proposed_fix": fix,
                "alternative_hypotheses": [],
            }
            transition(
                session,
                inc,
                S.PENDING_APPROVAL if fix else S.NO_ACTION,
                actor="agent",
                action="investigation_completed",
            )
            session.commit()
            return inc.id

    def get(self, incident_id: str) -> Incident:
        with Session(self.engine) as session:
            inc = session.get(Incident, incident_id)
            assert inc is not None
            return inc

    def audit(self, incident_id: str) -> list[AuditLog]:
        with Session(self.engine) as session:
            return list(
                session.exec(
                    select(AuditLog)
                    .where(AuditLog.incident_id == incident_id)
                    .order_by(col(AuditLog.id))
                )
            )


@pytest.fixture
def flow() -> Iterator[Flow]:
    fl = Flow()
    yield fl
    fl.engine.dispose()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_happy_path_dry_run_approve_apply(flow: Flow) -> None:
    iid = flow.incident()
    plan = run(flow.svc.dry_run(iid, actor="alice"))
    assert plan.dry_run is True and "+          memory: 512Mi" in plan.diff
    assert flow.executor.apps.applied == []
    assert flow.get(iid).dry_run is not None

    inc = run(flow.svc.approve(iid, actor="alice", comment="LGTM"))
    assert inc.status == S.APPLIED
    assert [p["body"] for p in flow.executor.apps.applied] == [plan.patch]
    assert inc.execution is not None and inc.execution["dry_run"] is False
    assert inc.rollout_status is not None and inc.rollout_status["generation"] == 6
    (approval,) = flow.svc.approvals(iid)
    assert approval.actor == "alice" and approval.comment == "LGTM"
    assert approval.consumed_at is not None

    trail = flow.audit(iid)
    assert [(a.action, a.actor, a.from_status, a.to_status) for a in trail] == [
        ("investigation_completed", "agent", S.INVESTIGATING, S.PENDING_APPROVAL),
        ("dry_run", "alice", None, None),
        ("approved", "alice", S.PENDING_APPROVAL, S.APPROVED),
        ("apply_started", "executor", S.APPROVED, S.APPLYING),
        ("applied", "executor", S.APPLYING, S.APPLIED),
    ]
    applied = trail[-1]
    assert applied.before is not None and applied.after is not None
    limits = applied.after["template"]["spec"]["containers"][0]["resources"]["limits"]
    assert limits == {"memory": "512Mi"}
    assert all(a.at is not None for a in trail)


def test_cannot_approve_before_dry_run(flow: Flow) -> None:
    iid = flow.incident()
    with pytest.raises(ApprovalError, match="dry run before approving"):
        run(flow.svc.approve(iid, actor="alice"))
    assert flow.get(iid).status == S.PENDING_APPROVAL


def test_action_cannot_run_without_approval(flow: Flow) -> None:
    iid = flow.incident()
    run(flow.svc.dry_run(iid, actor="alice"))
    # The internal apply path refuses a pending (unapproved) incident...
    with pytest.raises(ApprovalError, match="no unused approval"):
        run(flow.svc._apply(iid, "made-up-approval-id"))
    # ...and the executor refuses any call without a genuine token.
    action = ActionRequest.model_validate(flow.get(iid).action)
    with pytest.raises(ExecutorClientError):
        run(flow.client.execute(action, "not-a-token"))
    assert flow.executor.apps.applied == []
    assert flow.get(iid).status == S.PENDING_APPROVAL


def test_rejected_action_cannot_run(flow: Flow) -> None:
    iid = flow.incident()
    run(flow.svc.dry_run(iid, actor="alice"))
    inc = flow.svc.reject(iid, actor="bob", comment="needs a code fix instead")
    assert inc.status == S.REJECTED

    with pytest.raises(ApprovalError, match="only pending_approval can be approved"):
        run(flow.svc.approve(iid, actor="alice"))
    (decision,) = flow.svc.approvals(iid)
    assert decision.decision == "rejected"
    with pytest.raises(ApprovalError, match="no unused approval"):
        run(flow.svc._apply(iid, decision.id))
    assert flow.executor.apps.applied == []
    assert flow.audit(iid)[-1].action == "rejected"
    assert flow.audit(iid)[-1].detail == "needs a code fix instead"


def test_approval_cannot_be_reused(flow: Flow) -> None:
    iid = flow.incident()
    run(flow.svc.dry_run(iid, actor="alice"))
    run(flow.svc.approve(iid, actor="alice"))
    (approval,) = flow.svc.approvals(iid)

    with pytest.raises(ApprovalError):
        run(flow.svc.approve(iid, actor="alice"))
    with pytest.raises(ApprovalError, match="no unused approval"):
        run(flow.svc._apply(iid, approval.id))
    # Even replaying the exact token at the executor fails: it tracks used approvals itself.
    (token,) = flow.client.tokens
    action = ActionRequest.model_validate(flow.get(iid).action)
    with pytest.raises(ExecutorClientError, match="already been used"):
        run(flow.client.execute(action, token))
    assert len(flow.executor.apps.applied) == 1


def test_second_decision_is_refused(flow: Flow) -> None:
    iid = flow.incident()
    run(flow.svc.dry_run(iid, actor="alice"))
    flow.svc.reject(iid, actor="bob")
    with pytest.raises(ApprovalError):
        flow.svc.reject(iid, actor="carol")
    assert len(flow.svc.approvals(iid)) == 1


def test_non_allowlisted_namespace_is_refused(flow: Flow) -> None:
    fix = {**PATCH_FIX, "namespace": "kube-system"}
    iid = flow.incident(fix=fix, namespace="kube-system")
    with pytest.raises(ApprovalError, match="not in the executor allowlist"):
        run(flow.svc.dry_run(iid, actor="alice"))
    assert flow.get(iid).dry_run is None
    assert flow.audit(iid)[-1].action == "dry_run_refused"
    with pytest.raises(ApprovalError, match="dry run before approving"):
        run(flow.svc.approve(iid, actor="alice"))
    assert flow.executor.apps.patches == []


@pytest.mark.parametrize(
    "params",
    [
        {"container": "app", "memory_limit": "lots"},
        {"container": "app", "memory_limit": "64Gi"},
        {"container": "app", "memory_limit": "512Mi", "image": "attacker/miner:latest"},
    ],
)
def test_invalid_parameters_are_refused(flow: Flow, params: dict[str, Any]) -> None:
    iid = flow.incident(fix={**PATCH_FIX, "parameters": params})
    with pytest.raises(ApprovalError) as err:
        run(flow.svc.dry_run(iid, actor="alice"))
    assert err.value.code == "rejected"
    assert flow.executor.apps.patches == []


def test_executor_failure_marks_incident_failed(flow: Flow) -> None:
    iid = flow.incident()
    run(flow.svc.dry_run(iid, actor="alice"))
    flow.executor.apps.fail_patch = f.api_error(409, "the object has been modified")
    inc = run(flow.svc.approve(iid, actor="alice"))
    assert inc.status == S.FAILED
    assert inc.error is not None and "object has been modified" in inc.error
    assert flow.audit(iid)[-1].action == "apply_failed"


def test_dry_run_requires_pending_incident(flow: Flow) -> None:
    iid = flow.incident(fix=None)
    with pytest.raises(ApprovalError, match="not pending_approval"):
        run(flow.svc.dry_run(iid, actor="alice"))
    with pytest.raises(ApprovalError) as err:
        run(flow.svc.dry_run("missing", actor="alice"))
    assert err.value.code == "not_found"


def test_rollout_undo_resolves_replicaset_with_reader(cluster: FakeCluster) -> None:
    cluster.apps.read_namespaced_deployment.return_value = f.deployment(revision=3)
    cluster.apps.list_namespaced_replica_set.return_value = k.V1ReplicaSetList(
        items=[
            f.replicaset("checkout-v2", 2, "ghcr.io/acme/checkout:1.4.2"),
            f.replicaset("checkout-v3", 3, "ghcr.io/acme/checkout:bad"),
        ]
    )
    fl = Flow(reader=cluster.ctx)
    undo = {**PATCH_FIX, "action_type": "rollout_undo", "parameters": {}}
    iid = fl.incident(fix=undo)
    plan = run(fl.svc.dry_run(iid, actor="alice"))
    assert plan.action.parameters == {"to_revision": 2, "replicaset": "checkout-v2"}
    inc = run(fl.svc.approve(iid, actor="alice"))
    assert inc.status == S.APPLIED
    image = fl.executor.apps.applied[0]["body"][0]["value"]["spec"]["containers"][0]["image"]
    assert image == "ghcr.io/acme/checkout:1.4.2"
    assert inc.rollout_status is not None and "rollout_complete" in inc.rollout_status


def test_rollout_undo_without_previous_revision(cluster: FakeCluster) -> None:
    cluster.apps.read_namespaced_deployment.return_value = f.deployment(revision=1)
    cluster.apps.list_namespaced_replica_set.return_value = k.V1ReplicaSetList(
        items=[f.replicaset("checkout-v1", 1, "img:1")]
    )
    fl = Flow(reader=cluster.ctx)
    iid = fl.incident(fix={**PATCH_FIX, "action_type": "rollout_undo", "parameters": {}})
    with pytest.raises(ApprovalError, match="no revision previous"):
        run(fl.svc.dry_run(iid, actor="alice"))
    no_reader = Flow()
    iid2 = no_reader.incident(fix={**PATCH_FIX, "action_type": "rollout_undo", "parameters": {}})
    with pytest.raises(ApprovalError, match="needs cluster read access"):
        run(no_reader.svc.dry_run(iid2, actor="alice"))


# --- state machine & audit log --------------------------------------------------------------


def test_illegal_transitions_are_refused(flow: Flow) -> None:
    iid = flow.incident()
    with Session(flow.engine) as session:
        inc = session.get(Incident, iid)
        assert inc is not None
        for target in (S.APPLIED, S.APPLYING, S.INVESTIGATING, S.NO_ACTION):
            with pytest.raises(InvalidTransitionError):
                transition(session, inc, target, actor="x", action="x")


def test_concurrent_transition_loses(flow: Flow) -> None:
    iid = flow.incident()
    with Session(flow.engine) as a, Session(flow.engine) as b:
        inc_a, inc_b = a.get(Incident, iid), b.get(Incident, iid)
        assert inc_a is not None and inc_b is not None
        transition(a, inc_a, S.REJECTED, actor="bob", action="rejected")
        a.commit()
        with pytest.raises(InvalidTransitionError, match="cannot go from rejected"):
            transition(b, inc_b, S.APPROVED, actor="alice", action="approved")


@pytest.mark.parametrize("sql", ["UPDATE audit_log SET actor = 'mallory'", "DELETE FROM audit_log"])
def test_audit_log_is_append_only(flow: Flow, sql: str) -> None:
    flow.incident()
    with Session(flow.engine) as session, pytest.raises(DatabaseError, match="append-only"):
        session.exec(text(sql))  # type: ignore[call-overload]

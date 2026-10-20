"""Human approval gate: dry-run -> approve/reject -> (only then) executor apply.

Rules enforced here (and independently again by the executor):
* an action runs only for an incident in ``approved`` state with an unconsumed approval
* a rejected incident can never reach ``approved``
* one decision per incident; an approval is consumed exactly once
* the approved action is exactly the one that was dry-run (bound by its sha256 in the token)
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.agent.tools.base import ToolContext
from app.agent.tools.rollout import GetRolloutStatusInput, get_rollout_status
from app.executor.client import ExecutorClient, ExecutorClientError
from app.executor.schemas import ActionRequest, PlanResult
from app.executor.tokens import issue_token, new_claims
from app.models.db import Approval, Incident
from app.models.db import IncidentStatus as S
from app.models.state import audit, transition


class ApprovalError(Exception):
    """A request the approval flow refuses. ``code``: not_found | conflict | rejected."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class ApprovalService:
    def __init__(
        self,
        engine: Engine,
        executor: ExecutorClient,
        signing_key: bytes,
        *,
        reader: ToolContext | None = None,
        token_ttl_seconds: int = 300,
        rollout_wait_seconds: float = 60.0,
        rollout_poll_seconds: float = 3.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._engine = engine
        self._executor = executor
        self._key = signing_key
        self._reader = reader
        self._ttl = token_ttl_seconds
        self._rollout_wait = rollout_wait_seconds
        self._rollout_poll = rollout_poll_seconds
        self._clock = clock

    # --- dry run ------------------------------------------------------------------------

    async def dry_run(self, incident_id: str, actor: str) -> PlanResult:
        incident = self._load(incident_id)
        if incident.status != S.PENDING_APPROVAL:
            raise ApprovalError("conflict", f"incident is {incident.status}, not pending_approval")
        action = await self._prepare_action(incident)
        try:
            result = await self._executor.dry_run(action)
        except ExecutorClientError as exc:
            with Session(self._engine) as session:
                audit(
                    session,
                    actor=actor,
                    action="dry_run_refused",
                    incident_id=incident_id,
                    after={"action": action.model_dump(mode="json")},
                    detail=exc.detail,
                )
                session.commit()
            raise ApprovalError("rejected", f"executor refused the action: {exc.detail}") from exc

        with Session(self._engine) as session:
            stored = self._get(session, incident_id)
            stored.action = action.model_dump(mode="json")
            stored.dry_run = result.model_dump(mode="json")
            session.add(stored)
            audit(
                session,
                actor=actor,
                action="dry_run",
                incident_id=incident_id,
                before=result.before,
                after={"patch": result.patch, **result.after},
                detail=result.diff,
            )
            session.commit()
        return result

    # --- decisions ----------------------------------------------------------------------

    async def approve(self, incident_id: str, actor: str, comment: str | None = None) -> Incident:
        """Record the approval, then run the executor. Returns the final incident."""
        with Session(self._engine) as session:
            incident = self._get(session, incident_id)
            if incident.status != S.PENDING_APPROVAL:
                raise ApprovalError(
                    "conflict",
                    f"incident is {incident.status}; only pending_approval can be approved",
                )
            if incident.action is None or incident.dry_run is None:
                raise ApprovalError("conflict", "run a dry run before approving")
            action = ActionRequest.model_validate(incident.action)
            approval = Approval(
                incident_id=incident_id,
                decision="approved",
                actor=actor,
                comment=comment,
                action_sha256=action.digest(),
            )
            self._decide(session, incident, approval, S.APPROVED, "approved")
            approval_id = approval.id
        return await self._apply(incident_id, approval_id)

    def reject(self, incident_id: str, actor: str, comment: str | None = None) -> Incident:
        with Session(self._engine) as session:
            incident = self._get(session, incident_id)
            if incident.status != S.PENDING_APPROVAL:
                raise ApprovalError(
                    "conflict",
                    f"incident is {incident.status}; only pending_approval can be rejected",
                )
            action_sha = (
                ActionRequest.model_validate(incident.action).digest() if incident.action else ""
            )
            approval = Approval(
                incident_id=incident_id,
                decision="rejected",
                actor=actor,
                comment=comment,
                action_sha256=action_sha,
            )
            self._decide(session, incident, approval, S.REJECTED, "rejected")
            session.refresh(incident)
            return incident

    def _decide(
        self, session: Session, incident: Incident, approval: Approval, to: str, action: str
    ) -> None:
        session.add(approval)
        try:
            session.flush()  # unique incident_id: a second decision fails here
        except IntegrityError as exc:
            session.rollback()
            raise ApprovalError("conflict", "a decision was already recorded") from exc
        transition(
            session,
            incident,
            to,
            actor=approval.actor,
            action=action,
            detail=approval.comment,
            after={
                "status": to,
                "approval_id": approval.id,
                "action_sha256": approval.action_sha256,
            },
        )
        session.commit()

    # --- apply --------------------------------------------------------------------------

    async def _apply(self, incident_id: str, approval_id: str) -> Incident:
        with Session(self._engine) as session:
            incident = self._get(session, incident_id)
            approval = session.get(Approval, approval_id)
            if (
                incident.status != S.APPROVED
                or approval is None
                or approval.incident_id != incident_id
                or approval.decision != "approved"
                or approval.consumed_at is not None
                or incident.action is None
            ):
                raise ApprovalError("conflict", "no unused approval for this incident")
            action = ActionRequest.model_validate(incident.action)
            if action.digest() != approval.action_sha256:
                raise ApprovalError("conflict", "approved action does not match the stored action")
            approval.consumed_at = datetime.now(UTC)
            session.add(approval)
            transition(
                session,
                incident,
                S.APPLYING,
                actor="executor",
                action="apply_started",
                after={"status": S.APPLYING, "approval_id": approval_id},
            )
            session.commit()
            token = issue_token(
                new_claims(
                    approval_id=approval_id,
                    incident_id=incident_id,
                    action_sha256=approval.action_sha256,
                    approver=approval.actor,
                    ttl_seconds=self._ttl,
                    now=self._clock(),
                ),
                self._key,
            )

        try:
            result = await self._executor.execute(action, token)
        except ExecutorClientError as exc:
            return self._finish_failed(incident_id, f"executor: {exc.detail}")

        rollout = await self._wait_for_rollout(action)
        with Session(self._engine) as session:
            incident = self._get(session, incident_id)
            incident.execution = result.model_dump(mode="json")
            incident.rollout_status = rollout or (
                result.status.model_dump() if result.status else None
            )
            session.add(incident)
            transition(
                session,
                incident,
                S.APPLIED,
                actor="executor",
                action="applied",
                before=result.before,
                after=result.after,
                detail=result.diff,
            )
            session.commit()
            session.refresh(incident)
            return incident

    def _finish_failed(self, incident_id: str, error: str) -> Incident:
        with Session(self._engine) as session:
            incident = self._get(session, incident_id)
            incident.error = error
            session.add(incident)
            transition(
                session, incident, S.FAILED, actor="executor", action="apply_failed", detail=error
            )
            session.commit()
            session.refresh(incident)
            return incident

    async def _wait_for_rollout(self, action: ActionRequest) -> dict[str, Any] | None:
        """Poll rollout status with the *reader* identity until complete or timeout."""
        if self._reader is None:
            return None
        reader = self._reader
        params = GetRolloutStatusInput(namespace=action.namespace, deployment=action.target)
        deadline = time.monotonic() + self._rollout_wait
        status = await asyncio.to_thread(get_rollout_status, reader, params)
        while not status.rollout_complete and time.monotonic() < deadline:
            await asyncio.sleep(self._rollout_poll)
            status = await asyncio.to_thread(get_rollout_status, reader, params)
        return status.model_dump(mode="json")

    # --- helpers ------------------------------------------------------------------------

    async def _prepare_action(self, incident: Incident) -> ActionRequest:
        fix = (incident.report or {}).get("proposed_fix")
        if not fix:
            raise ApprovalError("conflict", "incident has no proposed fix")
        params = dict(fix.get("parameters") or {})
        if fix["action_type"] == "rollout_undo":
            params.update(await self._resolve_undo(fix["namespace"], fix["target"], params))
        try:
            return ActionRequest(
                action_type=fix["action_type"],
                namespace=fix["namespace"],
                target=fix["target"],
                parameters=params,
            )
        except ValueError as exc:
            raise ApprovalError("rejected", f"invalid proposed fix: {exc}") from exc

    async def _resolve_undo(
        self, namespace: str, deployment: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Find the ReplicaSet to roll back to (reader identity; the executor re-verifies)."""
        if self._reader is None:
            raise ApprovalError("rejected", "rollout_undo needs cluster read access to resolve")
        status = await asyncio.to_thread(
            get_rollout_status,
            self._reader,
            GetRolloutStatusInput(namespace=namespace, deployment=deployment),
        )
        wanted = params.get("to_revision")
        if wanted is None:
            target = status.previous_revision
        else:
            target = next((r for r in status.revision_history if r.revision == wanted), None)
        if target is None:
            raise ApprovalError("rejected", f"no revision {wanted or 'previous'} to roll back to")
        return {"to_revision": target.revision, "replicaset": target.replicaset}

    def _load(self, incident_id: str) -> Incident:
        with Session(self._engine) as session:
            return self._get(session, incident_id)

    @staticmethod
    def _get(session: Session, incident_id: str) -> Incident:
        incident = session.get(Incident, incident_id)
        if incident is None:
            raise ApprovalError("not_found", f"incident {incident_id} not found")
        return incident

    def approvals(self, incident_id: str) -> list[Approval]:
        with Session(self._engine) as session:
            return list(session.exec(select(Approval).where(Approval.incident_id == incident_id)))

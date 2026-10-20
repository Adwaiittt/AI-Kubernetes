"""``get_rollout_status``: deployment progress, ReplicaSet history, and a revision diff."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.agent.tools._format import iso, resources
from app.agent.tools.base import (
    Namespace,
    ObjectName,
    Tool,
    ToolContext,
    ToolInput,
    ToolOutput,
    cap_list,
)

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
MAX_HISTORY = 10


class GetRolloutStatusInput(ToolInput):
    namespace: Namespace
    deployment: ObjectName = Field(description="Deployment name.")


class Condition(ToolOutput):
    type: str
    status: str
    reason: str | None = None
    message: str | None = None


class Revision(ToolOutput):
    revision: int
    replicaset: str
    images: dict[str, str]  # container -> image
    replicas: int
    ready_replicas: int
    created_at: str | None


class ContainerChange(ToolOutput):
    container: str
    field: str  # image | resources | env | command | args | added | removed
    previous: Any = None
    current: Any = None


class GetRolloutStatusOutput(ToolOutput):
    deployment: str
    namespace: str
    desired_replicas: int
    updated_replicas: int
    ready_replicas: int
    available_replicas: int
    unavailable_replicas: int
    rollout_complete: bool
    paused: bool
    generation_observed: bool
    conditions: list[Condition]
    current_revision: Revision | None
    previous_revision: Revision | None
    changes_from_previous: list[ContainerChange]
    revision_history: list[Revision]


def _revision_of(obj: Any) -> int:
    try:
        return int((obj.metadata.annotations or {}).get(REVISION_ANNOTATION, 0))
    except (TypeError, ValueError):
        return 0


def _to_revision(rs: Any) -> Revision:
    return Revision(
        revision=_revision_of(rs),
        replicaset=rs.metadata.name,
        images={c.name: c.image for c in rs.spec.template.spec.containers},
        replicas=(rs.status.replicas if rs.status else 0) or 0,
        ready_replicas=(rs.status.ready_replicas if rs.status else 0) or 0,
        created_at=iso(rs.metadata.creation_timestamp),
    )


def _container_fields(c: Any) -> dict[str, Any]:
    return {
        "image": c.image,
        "resources": resources(c.resources),
        "env": sorted(e.name for e in c.env or []),
        "command": c.command,
        "args": c.args,
    }


def diff_templates(previous: Any, current: Any) -> list[ContainerChange]:
    """Container-level diff of two pod templates (image, resources, env names, command, args)."""
    prev = {c.name: _container_fields(c) for c in previous.spec.template.spec.containers}
    curr = {c.name: _container_fields(c) for c in current.spec.template.spec.containers}
    changes: list[ContainerChange] = []
    for name in sorted(prev.keys() | curr.keys()):
        if name not in curr:
            changes.append(ContainerChange(container=name, field="removed"))
        elif name not in prev:
            changes.append(ContainerChange(container=name, field="added"))
        else:
            for fld, before in prev[name].items():
                after = curr[name][fld]
                if before != after:
                    changes.append(
                        ContainerChange(container=name, field=fld, previous=before, current=after)
                    )
    return changes


def get_rollout_status(ctx: ToolContext, params: GetRolloutStatusInput) -> GetRolloutStatusOutput:
    timeout = ctx.clients.request_timeout
    dep = ctx.clients.apps.read_namespaced_deployment(
        params.deployment, params.namespace, _request_timeout=timeout
    )
    match_labels = (dep.spec.selector.match_labels or {}) if dep.spec.selector else {}
    selector = ",".join(f"{k}={v}" for k, v in sorted(match_labels.items()))
    rs_list = ctx.clients.apps.list_namespaced_replica_set(
        params.namespace, label_selector=selector, _request_timeout=timeout
    )
    owned = [
        rs
        for rs in rs_list.items
        if any(ref.uid == dep.metadata.uid for ref in rs.metadata.owner_references or [])
    ]
    owned.sort(key=_revision_of, reverse=True)

    dep_revision = _revision_of(dep)
    current = next((rs for rs in owned if _revision_of(rs) == dep_revision), None)
    if current is None and owned:
        current = owned[0]
    previous = next(
        (rs for rs in owned if current is not None and _revision_of(rs) < _revision_of(current)),
        None,
    )

    status = dep.status
    desired = dep.spec.replicas if dep.spec.replicas is not None else 1
    updated = (status.updated_replicas if status else 0) or 0
    ready = (status.ready_replicas if status else 0) or 0
    available = (status.available_replicas if status else 0) or 0
    observed = bool(status and (status.observed_generation or 0) >= (dep.metadata.generation or 0))
    history, _ = cap_list(owned, MAX_HISTORY)

    return GetRolloutStatusOutput(
        deployment=dep.metadata.name,
        namespace=dep.metadata.namespace,
        desired_replicas=desired,
        updated_replicas=updated,
        ready_replicas=ready,
        available_replicas=available,
        unavailable_replicas=(status.unavailable_replicas if status else 0) or 0,
        rollout_complete=observed and updated == desired and available == desired,
        paused=bool(dep.spec.paused),
        generation_observed=observed,
        conditions=[
            Condition(type=c.type, status=c.status, reason=c.reason, message=c.message)
            for c in (status.conditions if status else None) or []
        ],
        current_revision=_to_revision(current) if current is not None else None,
        previous_revision=_to_revision(previous) if previous is not None else None,
        changes_from_previous=(
            diff_templates(previous, current)
            if previous is not None and current is not None
            else []
        ),
        revision_history=[_to_revision(rs) for rs in history],
    )


GET_ROLLOUT_STATUS = Tool(
    name="get_rollout_status",
    description=(
        "Rollout status of a Deployment: desired/updated/ready/available replicas, conditions "
        "(e.g. ProgressDeadlineExceeded), the current and previous ReplicaSet revisions, revision "
        "history, and what changed between the previous and current revision (image, resources, "
        "env var names, command/args). Use it to spot a bad rollout."
    ),
    input_model=GetRolloutStatusInput,
    func=get_rollout_status,
)

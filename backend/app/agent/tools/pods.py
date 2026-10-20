"""``list_pods`` and ``describe_pod``."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.agent.tools._format import (
    ContainerStateSummary,
    ProbeSummary,
    container_state,
    iso,
    probe,
    resources,
)
from app.agent.tools.base import (
    Namespace,
    ObjectName,
    Tool,
    ToolContext,
    ToolInput,
    ToolOutput,
    cap_list,
)

# --- list_pods --------------------------------------------------------------------------------


class ListPodsInput(ToolInput):
    namespace: Namespace = Field(description="Namespace to list pods in.")
    label_selector: str | None = Field(
        default=None,
        max_length=512,
        description="Optional Kubernetes label selector, e.g. 'app=checkout,tier!=cache'.",
    )


class PodSummary(ToolOutput):
    name: str
    phase: str | None
    ready: str  # "ready/total"
    restarts: int
    status_reason: str | None = None  # e.g. CrashLoopBackOff, ImagePullBackOff, OOMKilled
    node: str | None = None
    created_at: str | None = None


class ListPodsOutput(ToolOutput):
    namespace: str
    pods: list[PodSummary]
    omitted: int = 0


def _status_reason(pod: Any) -> str | None:
    """The most informative short reason, mirroring kubectl's STATUS column."""
    status = pod.status
    if status is None:
        return None
    for cs in status.container_statuses or []:
        if cs.state and cs.state.waiting and cs.state.waiting.reason:
            return str(cs.state.waiting.reason)
        if cs.state and cs.state.terminated and cs.state.terminated.reason:
            return str(cs.state.terminated.reason)
    for cond in status.conditions or []:
        if cond.type == "PodScheduled" and cond.status == "False":
            return str(cond.reason or "Unschedulable")
    return str(status.reason) if status.reason else None


def _summarize(pod: Any) -> PodSummary:
    statuses = (pod.status.container_statuses if pod.status else None) or []
    total = len(pod.spec.containers) if pod.spec else len(statuses)
    return PodSummary(
        name=pod.metadata.name,
        phase=pod.status.phase if pod.status else None,
        ready=f"{sum(1 for cs in statuses if cs.ready)}/{total}",
        restarts=sum(cs.restart_count or 0 for cs in statuses),
        status_reason=_status_reason(pod),
        node=pod.spec.node_name if pod.spec else None,
        created_at=iso(pod.metadata.creation_timestamp),
    )


def list_pods(ctx: ToolContext, params: ListPodsInput) -> ListPodsOutput:
    kwargs: dict[str, Any] = {"_request_timeout": ctx.clients.request_timeout}
    if params.label_selector:
        kwargs["label_selector"] = params.label_selector
    result = ctx.clients.core.list_namespaced_pod(params.namespace, **kwargs)
    pods = sorted(result.items, key=lambda p: p.metadata.name)
    kept, omitted = cap_list(pods, ctx.settings.list_max_items)
    return ListPodsOutput(
        namespace=params.namespace, pods=[_summarize(p) for p in kept], omitted=omitted
    )


LIST_PODS = Tool(
    name="list_pods",
    description=(
        "List pods in a namespace with phase, ready count, total restarts, the kubectl-style "
        "status reason (e.g. CrashLoopBackOff, ImagePullBackOff, Pending/Unschedulable) and node. "
        "Start here to find unhealthy pods."
    ),
    input_model=ListPodsInput,
    func=list_pods,
)


# --- describe_pod -----------------------------------------------------------------------------


class DescribePodInput(ToolInput):
    namespace: Namespace
    name: ObjectName = Field(description="Pod name.")


class ContainerDetail(ToolOutput):
    name: str
    init: bool = False
    image: str | None
    ready: bool | None = None
    restart_count: int | None = None
    state: ContainerStateSummary | None = None
    last_termination: ContainerStateSummary | None = None
    resources: dict[str, dict[str, str]] = Field(default_factory=dict)
    liveness_probe: ProbeSummary | None = None
    readiness_probe: ProbeSummary | None = None
    startup_probe: ProbeSummary | None = None
    ports: list[int] = Field(default_factory=list)
    command: list[str] | None = None
    args: list[str] | None = None
    # Only env var *names* and their source kind. Values may be secrets, so they are never shown.
    env: dict[str, str] = Field(default_factory=dict)


class PodCondition(ToolOutput):
    type: str
    status: str
    reason: str | None = None
    message: str | None = None


class DescribePodOutput(ToolOutput):
    name: str
    namespace: str
    phase: str | None
    node: str | None
    qos_class: str | None
    owner: str | None  # "ReplicaSet/checkout-7d9f"
    labels: dict[str, str]
    conditions: list[PodCondition]
    containers: list[ContainerDetail]


def _env_summary(container: Any) -> dict[str, str]:
    env: dict[str, str] = {}
    for var in container.env or []:
        if var.value_from is None:
            env[var.name] = "literal"
        elif var.value_from.secret_key_ref is not None:
            env[var.name] = f"secretKeyRef:{var.value_from.secret_key_ref.name}"
        elif var.value_from.config_map_key_ref is not None:
            env[var.name] = f"configMapKeyRef:{var.value_from.config_map_key_ref.name}"
        else:
            env[var.name] = "valueFrom"
    for src in container.env_from or []:
        if src.secret_ref is not None:
            env[f"<envFrom secret {src.secret_ref.name}>"] = "envFrom"
        if src.config_map_ref is not None:
            env[f"<envFrom configMap {src.config_map_ref.name}>"] = "envFrom"
    return env


def _container_detail(spec: Any, status: Any, init: bool) -> ContainerDetail:
    return ContainerDetail(
        name=spec.name,
        init=init,
        image=spec.image,
        ready=status.ready if status else None,
        restart_count=status.restart_count if status else None,
        state=container_state(status.state) if status else None,
        last_termination=container_state(status.last_state) if status else None,
        resources=resources(spec.resources),
        liveness_probe=probe(spec.liveness_probe),
        readiness_probe=probe(spec.readiness_probe),
        startup_probe=probe(spec.startup_probe),
        ports=[p.container_port for p in spec.ports or []],
        command=spec.command,
        args=spec.args,
        env=_env_summary(spec),
    )


def describe_pod(ctx: ToolContext, params: DescribePodInput) -> DescribePodOutput:
    pod = ctx.clients.core.read_namespaced_pod(
        params.name, params.namespace, _request_timeout=ctx.clients.request_timeout
    )
    status = pod.status
    by_name = {cs.name: cs for cs in (status.container_statuses if status else None) or []}
    init_by_name = {
        cs.name: cs for cs in (status.init_container_statuses if status else None) or []
    }
    containers = [
        _container_detail(c, init_by_name.get(c.name), True) for c in pod.spec.init_containers or []
    ]
    containers += [_container_detail(c, by_name.get(c.name), False) for c in pod.spec.containers]
    owner = next(iter(pod.metadata.owner_references or []), None)
    return DescribePodOutput(
        name=pod.metadata.name,
        namespace=pod.metadata.namespace,
        phase=status.phase if status else None,
        node=pod.spec.node_name,
        qos_class=status.qos_class if status else None,
        owner=f"{owner.kind}/{owner.name}" if owner else None,
        labels=dict(pod.metadata.labels or {}),
        conditions=[
            PodCondition(type=c.type, status=c.status, reason=c.reason, message=c.message)
            for c in (status.conditions if status else None) or []
        ],
        containers=containers,
    )


DESCRIBE_POD = Tool(
    name="describe_pod",
    description=(
        "Describe one pod: per-container current state and LAST termination (reason such as "
        "OOMKilled/Error, exit code, timestamps), restart count, image, resource requests/limits, "
        "liveness/readiness/startup probes, ports, command/args, env var names (values hidden), "
        "and pod conditions (e.g. scheduling failures)."
    ),
    input_model=DescribePodInput,
    func=describe_pod,
)

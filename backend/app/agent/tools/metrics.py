"""``get_metrics``: live CPU/memory from metrics-server, side by side with requests/limits."""

from __future__ import annotations

from typing import Any

from kubernetes.client.exceptions import ApiException
from pydantic import Field

from app.agent.tools._format import cpu_millicores, memory_mib
from app.agent.tools.base import (
    Namespace,
    ObjectName,
    Tool,
    ToolContext,
    ToolInput,
    ToolOutput,
    cap_list,
    format_api_error,
)

METRICS_GROUP, METRICS_VERSION = "metrics.k8s.io", "v1beta1"


class GetMetricsInput(ToolInput):
    namespace: Namespace
    pod: ObjectName | None = Field(default=None, description="Limit to one pod.")


class ContainerUsage(ToolOutput):
    container: str
    cpu_usage_millicores: float | None
    cpu_request_millicores: float | None = None
    cpu_limit_millicores: float | None = None
    cpu_pct_of_limit: float | None = None
    memory_usage_mib: float | None
    memory_request_mib: float | None = None
    memory_limit_mib: float | None = None
    memory_pct_of_limit: float | None = None


class PodUsage(ToolOutput):
    pod: str
    window: str | None = None
    containers: list[ContainerUsage]


class GetMetricsOutput(ToolOutput):
    namespace: str
    available: bool
    message: str | None = None
    pods: list[PodUsage] = Field(default_factory=list)
    omitted: int = 0


def _pct(usage: float | None, limit: float | None) -> float | None:
    if usage is None or not limit:
        return None
    return round(100 * usage / limit, 1)


def _pod_specs(ctx: ToolContext, namespace: str, pod: str | None) -> dict[str, dict[str, Any]]:
    """{pod: {container: V1ResourceRequirements}} so usage can be shown next to limits."""
    timeout = ctx.clients.request_timeout
    try:
        if pod:
            items = [ctx.clients.core.read_namespaced_pod(pod, namespace, _request_timeout=timeout)]
        else:
            items = ctx.clients.core.list_namespaced_pod(namespace, _request_timeout=timeout).items
    except ApiException:
        return {}
    return {p.metadata.name: {c.name: c.resources for c in p.spec.containers} for p in items}


def _unavailable(namespace: str, exc: ApiException) -> GetMetricsOutput | None:
    """Translate 'metrics API missing / down' into a clear, non-error answer."""
    body = str(exc.body or "")
    pod_not_found = exc.status == 404 and "podmetrics" in body.lower()
    if exc.status in (404, 503) and not pod_not_found:
        return GetMetricsOutput(
            namespace=namespace,
            available=False,
            message=(
                "metrics-server is not installed or not ready (metrics.k8s.io API "
                f"unavailable: {format_api_error(exc)}). Live usage can't be checked; use "
                "describe_pod for limits/last termination reason and get_events for OOM kills."
            ),
        )
    if pod_not_found:
        return GetMetricsOutput(
            namespace=namespace,
            available=True,
            message="No metrics for this pod yet (it may not be running, or was just started).",
        )
    return None


def get_metrics(ctx: ToolContext, params: GetMetricsInput) -> GetMetricsOutput:
    timeout = ctx.clients.request_timeout
    try:
        if params.pod:
            raw = ctx.clients.custom.get_namespaced_custom_object(
                METRICS_GROUP,
                METRICS_VERSION,
                params.namespace,
                "pods",
                params.pod,
                _request_timeout=timeout,
            )
            items = [raw]
        else:
            raw = ctx.clients.custom.list_namespaced_custom_object(
                METRICS_GROUP,
                METRICS_VERSION,
                params.namespace,
                "pods",
                _request_timeout=timeout,
            )
            items = raw.get("items", [])
    except ApiException as exc:
        degraded = _unavailable(params.namespace, exc)
        if degraded is None:
            raise
        return degraded

    specs = _pod_specs(ctx, params.namespace, params.pod)
    items = sorted(items, key=lambda m: m["metadata"]["name"])
    kept, omitted = cap_list(items, ctx.settings.list_max_items)
    pods: list[PodUsage] = []
    for item in kept:
        name = item["metadata"]["name"]
        container_specs = specs.get(name, {})
        usages: list[ContainerUsage] = []
        for c in item.get("containers", []):
            usage = c.get("usage", {})
            res = container_specs.get(c["name"])
            requests = (res.requests if res else None) or {}
            limits = (res.limits if res else None) or {}
            cpu = cpu_millicores(usage.get("cpu"))
            mem = memory_mib(usage.get("memory"))
            cpu_limit = cpu_millicores(limits.get("cpu"))
            mem_limit = memory_mib(limits.get("memory"))
            usages.append(
                ContainerUsage(
                    container=c["name"],
                    cpu_usage_millicores=cpu,
                    cpu_request_millicores=cpu_millicores(requests.get("cpu")),
                    cpu_limit_millicores=cpu_limit,
                    cpu_pct_of_limit=_pct(cpu, cpu_limit),
                    memory_usage_mib=mem,
                    memory_request_mib=memory_mib(requests.get("memory")),
                    memory_limit_mib=mem_limit,
                    memory_pct_of_limit=_pct(mem, mem_limit),
                )
            )
        pods.append(PodUsage(pod=name, window=item.get("window"), containers=usages))
    return GetMetricsOutput(namespace=params.namespace, available=True, pods=pods, omitted=omitted)


GET_METRICS = Tool(
    name="get_metrics",
    description=(
        "Current CPU (millicores) and memory (MiB) usage per container from metrics-server, "
        "shown next to requests/limits with % of limit. Useful for memory pressure / OOM and CPU "
        "throttling. Returns available=false with an explanation if metrics-server is absent."
    ),
    input_model=GetMetricsInput,
    func=get_metrics,
)

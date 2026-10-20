"""``get_pod_logs``."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.agent.tools.base import (
    Namespace,
    ObjectName,
    Tool,
    ToolContext,
    ToolInput,
    ToolOutput,
    truncate_tail,
)


class GetPodLogsInput(ToolInput):
    namespace: Namespace
    name: ObjectName = Field(description="Pod name.")
    container: Namespace | None = Field(
        default=None, description="Container name. Required when the pod has several containers."
    )
    previous: bool = Field(
        default=False,
        description=(
            "Read logs of the PREVIOUS (crashed/restarted) container instance. Use this for "
            "CrashLoopBackOff: the current instance often has no useful output yet."
        ),
    )
    tail_lines: int = Field(default=200, ge=1, le=1000, description="Lines from the end.")


class GetPodLogsOutput(ToolOutput):
    pod: str
    container: str | None
    previous: bool
    line_count: int
    truncated: bool
    logs: str
    available: bool = True
    note: str | None = None


# The kubelet answers 200 OK with this text (not an error status) when it can no longer read a
# container's log file, e.g. the instance was already garbage-collected in a fast crash loop.
_KUBELET_UNAVAILABLE = "unable to retrieve container logs for"


def get_pod_logs(ctx: ToolContext, params: GetPodLogsInput) -> GetPodLogsOutput:
    kwargs: dict[str, Any] = {
        "tail_lines": min(params.tail_lines, ctx.settings.log_max_lines),
        "previous": params.previous,
        "_request_timeout": ctx.clients.request_timeout,
    }
    if params.container:
        kwargs["container"] = params.container
    raw = ctx.clients.core.read_namespaced_pod_log(params.name, params.namespace, **kwargs)
    text = raw if isinstance(raw, str) else str(raw)
    if text.strip().startswith(_KUBELET_UNAVAILABLE) and len(text.splitlines()) == 1:
        return GetPodLogsOutput(
            pod=params.name,
            container=params.container,
            previous=params.previous,
            line_count=0,
            truncated=False,
            logs="",
            available=False,
            note=(
                f"The kubelet could not retrieve these logs ({text.strip()}). This is NOT "
                "application output. The container instance was likely already garbage-collected "
                "(common in fast crash loops). Try again, try the other value of `previous`, "
                "or rely on describe_pod's last termination reason and exit code."
            ),
        )
    result = truncate_tail(text, ctx.settings.log_max_lines, ctx.settings.log_max_bytes)
    return GetPodLogsOutput(
        pod=params.name,
        container=params.container,
        previous=params.previous,
        line_count=len(text.splitlines()),
        truncated=result.truncated,
        logs=result.text if text.strip() else "<no log output>",
    )


GET_POD_LOGS = Tool(
    name="get_pod_logs",
    description=(
        "Fetch the tail of a container's logs. Set previous=true to read the logs of the last "
        "crashed instance (essential for CrashLoopBackOff). Output is truncated to keep the most "
        "recent lines."
    ),
    input_model=GetPodLogsInput,
    func=get_pod_logs,
)

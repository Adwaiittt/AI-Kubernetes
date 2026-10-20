"""The six read-only investigation tools."""

from app.agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from app.agent.tools.events import GET_EVENTS
from app.agent.tools.logs import GET_POD_LOGS
from app.agent.tools.metrics import GET_METRICS
from app.agent.tools.pods import DESCRIBE_POD, LIST_PODS
from app.agent.tools.rollout import GET_ROLLOUT_STATUS

READ_ONLY_TOOLS: tuple[Tool, ...] = (
    LIST_PODS,
    DESCRIBE_POD,
    GET_POD_LOGS,
    GET_EVENTS,
    GET_ROLLOUT_STATUS,
    GET_METRICS,
)


def build_registry() -> ToolRegistry:
    return ToolRegistry(READ_ONLY_TOOLS)


__all__ = ["READ_ONLY_TOOLS", "Tool", "ToolContext", "ToolRegistry", "ToolResult", "build_registry"]

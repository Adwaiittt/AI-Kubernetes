"""Tool registry, shared input types, output truncation, and error formatting.

Every investigation tool is a plain function ``(ToolContext, InputModel) -> OutputModel``.
The registry turns that into a Claude API tool definition (name, description, JSON schema) and
executes raw model-supplied input safely: validation errors, Kubernetes API errors, and any
unexpected exception become an ``is_error`` result instead of propagating.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from kubernetes.client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from app.core.config import Settings
from app.core.k8s import ReadOnlyClients

# DNS-1123 label (namespaces, container names) and subdomain (most object names).
Namespace = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
]
ObjectName = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$",
        max_length=253,
    ),
]

TRUNCATION_MARKER = "[truncated: {n} earlier lines omitted]"


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolOutput(BaseModel):
    model_config = ConfigDict(frozen=True)


@dataclass(frozen=True)
class ToolContext:
    clients: ReadOnlyClients
    settings: Settings


class ToolResult(BaseModel):
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[ToolInput]
    func: Callable[[ToolContext, Any], ToolOutput]

    def definition(self) -> dict[str, Any]:
        """Tool definition in the shape the Anthropic Messages API expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _strip_titles(self.input_model.model_json_schema()),
        }


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"duplicate tool name: {tool.name}")
            self._tools[tool.name] = tool

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def definitions(self) -> list[dict[str, Any]]:
        return [tool.definition() for tool in self._tools.values()]

    def execute(self, name: str, raw_input: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                content=f"Unknown tool {name!r}. Available tools: {', '.join(self._tools)}",
                is_error=True,
            )
        try:
            params = tool.input_model.model_validate(raw_input)
        except ValidationError as exc:
            return ToolResult(
                content=f"Invalid input for {name}: {_format_validation(exc)}", is_error=True
            )
        try:
            output = tool.func(ctx, params)
        except ApiException as exc:
            return ToolResult(content=format_api_error(exc), is_error=True)
        except Exception as exc:  # tool bugs must not crash an investigation
            return ToolResult(content=f"{name} failed: {type(exc).__name__}: {exc}", is_error=True)
        return ToolResult(
            content=cap_bytes(
                output.model_dump_json(exclude_none=True), ctx.settings.tool_output_max_bytes
            )
        )


# --- truncation -------------------------------------------------------------------------------


@dataclass(frozen=True)
class TruncatedText:
    text: str
    truncated: bool
    omitted_lines: int


def truncate_tail(text: str, max_lines: int, max_bytes: int) -> TruncatedText:
    """Keep the *last* lines of ``text`` (most recent log output) within line and byte caps."""
    lines = text.splitlines()
    omitted = max(0, len(lines) - max_lines)
    kept = lines[omitted:]

    size = 0
    start = len(kept)
    for i in range(len(kept) - 1, -1, -1):
        line_size = len(kept[i].encode("utf-8")) + 1
        if size + line_size > max_bytes:
            break
        size += line_size
        start = i
    if start == len(kept) and kept:
        # A single final line is larger than the budget: keep its tail.
        last = kept[-1].encode("utf-8")[-max_bytes:].decode("utf-8", errors="ignore")
        omitted += len(kept) - 1
        kept = [last]
        return TruncatedText(_with_marker(kept, omitted), True, omitted)
    omitted += start
    kept = kept[start:]
    return TruncatedText(_with_marker(kept, omitted), omitted > 0, omitted)


def _with_marker(lines: list[str], omitted: int) -> str:
    if omitted:
        return "\n".join([TRUNCATION_MARKER.format(n=omitted), *lines])
    return "\n".join(lines)


def cap_list[T](items: Sequence[T], max_items: int) -> tuple[list[T], int]:
    """Return at most ``max_items`` items plus how many were dropped."""
    return list(items[:max_items]), max(0, len(items) - max_items)


def cap_bytes(text: str, max_bytes: int) -> str:
    """Last-resort guard on the serialized tool result."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    marker = f"\n[output truncated: {len(raw) - max_bytes} bytes omitted]"
    return raw[:max_bytes].decode("utf-8", errors="ignore") + marker


# --- error formatting -------------------------------------------------------------------------


def format_api_error(exc: ApiException) -> str:
    message = exc.reason or "error"
    if exc.body:
        try:
            body = json.loads(exc.body)
            message = body.get("message", message)
        except (ValueError, TypeError, AttributeError):
            message = str(exc.body)[:500]
    return f"Kubernetes API error {exc.status}: {message}"


def _format_validation(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'input'}: {err['msg']}" for err in exc.errors()
    )


def _strip_titles(schema: Any) -> Any:
    if isinstance(schema, dict):
        # Only drop schema "title" annotations (strings), never a property *named* "title".
        return {
            k: _strip_titles(v)
            for k, v in schema.items()
            if not (k == "title" and isinstance(v, str))
        }
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    return schema

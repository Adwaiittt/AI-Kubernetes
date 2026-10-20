"""Manual Claude tool-use loop for an investigation.

Flow: send the incident + tool definitions -> run every ``tool_use`` block -> send the
``tool_result`` blocks back -> repeat until the model calls ``submit_report`` with a valid report.

Guardrails:
* at most ``max_tool_calls`` investigation tool executions; calls past the cap are not run and
  get an ``is_error`` result telling the model to submit
* a per-call timeout around each tool, a cap on model round-trips, and a total token budget
* tool failures go back to the model as ``is_error`` results; they never crash the loop
* an invalid report is returned to the model with the validation errors, ``report_retries`` times

History is append-only (assistant content is passed back unchanged, the system prompt and tool
list never change), which keeps both the prompt cache and the model's thinking blocks valid.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from app.agent.prompts import (
    BUDGET_EXHAUSTED,
    NUDGE_SUBMIT,
    IncidentPrompt,
    incident_message,
    system_prompt,
)
from app.agent.report import SUBMIT_REPORT, RootCauseReport, submit_report_tool
from app.agent.tools.base import ToolContext, ToolRegistry, ToolResult
from app.core.config import Effort, Settings

REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"

EventType = Literal[
    "investigation_started",
    "model_response",
    "tool_call",
    "tool_result",
    "report_rejected",
    "report_accepted",
    "nudge",
    "investigation_failed",
]


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentConfig:
    model: str
    effort: Effort = "medium"
    max_tool_calls: int = 15
    max_turns: int = 25
    tool_timeout_seconds: float = 20.0
    token_budget: int = 600_000
    max_tokens: int = 16_000
    report_retries: int = 1
    refusal_fallback: bool = True

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentConfig:
        if not settings.anthropic_model:
            raise ConfigurationError("ANTHROPIC_MODEL is not set")
        return cls(
            model=settings.anthropic_model,
            effort=settings.anthropic_effort,
            max_tool_calls=settings.agent_max_tool_calls,
            max_turns=settings.agent_max_turns,
            tool_timeout_seconds=settings.agent_tool_timeout_seconds,
            token_budget=settings.agent_token_budget,
            max_tokens=settings.agent_max_tokens,
            report_retries=settings.agent_report_retries,
            refusal_fallback=settings.anthropic_refusal_fallback,
        )


def build_anthropic_client(settings: Settings) -> anthropic.AsyncAnthropic:
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    # api_key=None lets the SDK resolve credentials from the environment itself.
    return anthropic.AsyncAnthropic(api_key=key, max_retries=3)


class AgentEvent(BaseModel):
    seq: int
    type: EventType
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    data: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    def add(self, usage: Any) -> None:
        for name in type(self).model_fields:
            setattr(self, name, getattr(self, name) + (getattr(usage, name, 0) or 0))


class InvestigationResult(BaseModel):
    status: Literal["completed", "failed"]
    report: RootCauseReport | None = None
    error: str | None = None
    model: str
    tool_calls: int
    model_turns: int
    usage: TokenUsage
    latency_ms: int
    transcript: list[AgentEvent]


EventSink = Callable[[AgentEvent], Awaitable[None]]


async def _discard(_event: AgentEvent) -> None:
    return None


class InvestigationFailedError(Exception):
    pass


@dataclass
class _RunState:
    tool_calls: int = 0
    turns: int = 0
    report_failures: int = 0
    nudged: bool = False
    called_tools: set[str] = field(default_factory=set)
    usage: TokenUsage = field(default_factory=TokenUsage)
    transcript: list[AgentEvent] = field(default_factory=list)


class Investigator:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        registry: ToolRegistry,
        ctx: ToolContext,
        config: AgentConfig,
    ) -> None:
        self._client = client
        self._registry = registry
        self._ctx = ctx
        self._config = config
        # Built once: byte-identical across requests so the cached prefix holds.
        self._tools = [*registry.definitions(), submit_report_tool()]
        self._system = [
            {
                "type": "text",
                "text": system_prompt(config.max_tool_calls),
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def run(
        self, incident: IncidentPrompt, on_event: EventSink = _discard
    ) -> InvestigationResult:
        state = _RunState()
        started = time.monotonic()

        async def emit(etype: EventType, **data: Any) -> None:
            event = AgentEvent(seq=len(state.transcript), type=etype, data=data)
            state.transcript.append(event)
            await on_event(event)

        def result(
            status: Literal["completed", "failed"],
            report: RootCauseReport | None = None,
            error: str | None = None,
        ) -> InvestigationResult:
            return InvestigationResult(
                status=status,
                report=report,
                error=error,
                model=self._config.model,
                tool_calls=state.tool_calls,
                model_turns=state.turns,
                usage=state.usage,
                latency_ms=int((time.monotonic() - started) * 1000),
                transcript=state.transcript,
            )

        await emit(
            "investigation_started",
            namespace=incident.namespace,
            target=incident.target,
            symptom=incident.symptom,
            model=self._config.model,
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": incident_message(incident)}]
        try:
            report = await self._loop(incident, messages, state, emit)
        except InvestigationFailedError as exc:
            await emit("investigation_failed", error=str(exc))
            return result("failed", error=str(exc))
        return result("completed", report=report)

    async def _loop(
        self,
        incident: IncidentPrompt,
        messages: list[dict[str, Any]],
        state: _RunState,
        emit: Callable[..., Awaitable[None]],
    ) -> RootCauseReport:
        cfg = self._config
        while True:
            if state.turns >= cfg.max_turns:
                raise InvestigationFailedError(f"stopped after {cfg.max_turns} model turns")
            if state.usage.total >= cfg.token_budget:
                raise InvestigationFailedError(
                    f"token budget exhausted ({state.usage.total} >= {cfg.token_budget})"
                )

            response = await self._call_model(messages)
            state.turns += 1
            state.usage.add(response.usage)
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            await emit(
                "model_response",
                stop_reason=response.stop_reason,
                text=[b.text for b in response.content if b.type == "text" and b.text],
                tool_uses=[b.name for b in tool_uses],
                usage=TokenUsage().model_copy(update=_usage_dict(response.usage)).model_dump(),
            )

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                category = getattr(details, "category", None)
                raise InvestigationFailedError(f"model declined the request (category={category})")
            if response.stop_reason == "max_tokens":
                raise InvestigationFailedError("model response hit max_tokens")

            messages.append({"role": "assistant", "content": response.content})

            if not tool_uses:
                if state.nudged:
                    raise InvestigationFailedError("model stopped without submitting a report")
                state.nudged = True
                await emit("nudge", message=NUDGE_SUBMIT)
                messages.append({"role": "user", "content": NUDGE_SUBMIT})
                continue

            results: list[dict[str, Any]] = []
            accepted: RootCauseReport | None = None
            for block in tool_uses:
                if block.name == SUBMIT_REPORT:
                    outcome = await self._handle_report(block.input, incident, state, emit)
                    if isinstance(outcome, RootCauseReport):
                        accepted = outcome
                        results.append(_tool_result(block.id, "Report accepted."))
                    else:
                        results.append(_tool_result(block.id, outcome, is_error=True))
                else:
                    results.append(await self._handle_tool(block, state, emit))

            if accepted is not None:
                return accepted
            if state.report_failures > cfg.report_retries:
                raise InvestigationFailedError(
                    f"report failed validation {state.report_failures} times"
                )
            messages.append({"role": "user", "content": results})

    async def _call_model(self, messages: list[dict[str, Any]]) -> Any:
        cfg = self._config
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "system": self._system,
            "tools": self._tools,
            "messages": messages,
            "output_config": {"effort": cfg.effort},
            # Automatic breakpoint on the growing transcript; the explicit one on the system
            # block caches tools + system.
            "cache_control": {"type": "ephemeral"},
        }
        if cfg.refusal_fallback:
            kwargs["betas"] = [REFUSAL_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            return await self._client.beta.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            raise InvestigationFailedError(
                f"Claude API error {exc.status_code}: {exc.message}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise InvestigationFailedError(f"Claude API unreachable: {exc}") from exc

    async def _handle_tool(
        self, block: Any, state: _RunState, emit: Callable[..., Awaitable[None]]
    ) -> dict[str, Any]:
        cfg = self._config
        args = block.input if isinstance(block.input, dict) else {}
        await emit("tool_call", tool_use_id=block.id, tool=block.name, input=args)

        duration_ms = 0
        if state.tool_calls >= cfg.max_tool_calls:
            outcome = ToolResult(
                content=BUDGET_EXHAUSTED.format(max_tool_calls=cfg.max_tool_calls), is_error=True
            )
            executed = False
        else:
            state.tool_calls += 1
            state.called_tools.add(block.name)
            executed = True
            started = time.monotonic()
            try:
                outcome = await asyncio.wait_for(
                    asyncio.to_thread(self._registry.execute, block.name, args, self._ctx),
                    timeout=cfg.tool_timeout_seconds,
                )
            except TimeoutError:
                outcome = ToolResult(
                    content=f"{block.name} timed out after {cfg.tool_timeout_seconds:g}s",
                    is_error=True,
                )
            duration_ms = int((time.monotonic() - started) * 1000)

        await emit(
            "tool_result",
            tool_use_id=block.id,
            tool=block.name,
            is_error=outcome.is_error,
            executed=executed,
            duration_ms=duration_ms,
            content=outcome.content,
        )
        return _tool_result(block.id, outcome.content, is_error=outcome.is_error)

    async def _handle_report(
        self,
        raw: Any,
        incident: IncidentPrompt,
        state: _RunState,
        emit: Callable[..., Awaitable[None]],
    ) -> RootCauseReport | str:
        try:
            report = RootCauseReport.model_validate(raw)
            problems = _grounding_problems(report, incident, state.called_tools)
        except ValidationError as exc:
            problems = [
                f"{'.'.join(str(p) for p in err['loc']) or 'report'}: {err['msg']}"
                for err in exc.errors()
            ]
        if not problems:
            await emit("report_accepted", report=report.model_dump(mode="json"))
            return report

        state.report_failures += 1
        remaining = self._config.report_retries - state.report_failures + 1
        await emit("report_rejected", problems=problems, attempt=state.report_failures)
        return (
            "The report was rejected:\n- "
            + "\n- ".join(problems)
            + f"\nFix these problems and call submit_report again ({max(remaining, 0)} "
            "attempt(s) left)."
        )


def _grounding_problems(
    report: RootCauseReport, incident: IncidentPrompt, called_tools: set[str]
) -> list[str]:
    problems = [
        f"evidence[{i}] cites {ev.tool_name}, which was not called in this investigation"
        for i, ev in enumerate(report.evidence)
        if ev.tool_name not in called_tools
    ]
    fix = report.proposed_fix
    if fix is not None and fix.namespace != incident.namespace:
        problems.append(
            f"proposed_fix.namespace is {fix.namespace!r}, but the investigated namespace is "
            f"{incident.namespace!r}"
        )
    return problems


def _tool_result(tool_use_id: str, content: str, is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block


def _usage_dict(usage: Any) -> dict[str, int]:
    return {name: getattr(usage, name, 0) or 0 for name in TokenUsage.model_fields}

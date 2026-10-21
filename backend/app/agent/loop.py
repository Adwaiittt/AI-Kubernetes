"""Manual tool-use loop for an investigation (provider-neutral: Claude or Gemini).

Flow: send the incident + tool definitions -> run every tool call -> send the results back ->
repeat until the model calls ``submit_report`` with a valid report.

Guardrails:
* at most ``max_tool_calls`` investigation tool executions; calls past the cap are not run and
  get an error result telling the model to submit
* a per-call timeout around each tool, a cap on model round-trips, and a total token budget
* tool failures go back to the model as error results; they never crash the loop
* an invalid report is returned to the model with the validation errors, ``report_retries`` times

History is append-only (each provider replays the model's turns unchanged and never changes the
system prompt or tool list), which keeps caches and the models' reasoning state valid.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from app.agent.config import AgentConfig, ConfigurationError
from app.agent.prompts import (
    BUDGET_EXHAUSTED,
    NUDGE_SUBMIT,
    IncidentPrompt,
    incident_message,
    system_prompt,
)
from app.agent.providers.anthropic import (
    REFUSAL_FALLBACK_BETA,
    AnthropicProvider,
    build_anthropic_client,
)
from app.agent.providers.base import (
    Conversation,
    ModelProvider,
    ProviderError,
    ToolCall,
    ToolResultMessage,
)
from app.agent.report import SUBMIT_REPORT, RootCauseReport, submit_report_tool
from app.agent.tools.base import ToolContext, ToolRegistry, ToolResult

__all__ = [
    "REFUSAL_FALLBACK_BETA",
    "AgentConfig",
    "AgentEvent",
    "ConfigurationError",
    "InvestigationResult",
    "Investigator",
    "TokenUsage",
    "build_anthropic_client",
]

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

    def add(self, usage: Mapping[str, int] | Any) -> None:
        for name in type(self).model_fields:
            value = usage.get(name, 0) if isinstance(usage, Mapping) else getattr(usage, name, 0)
            setattr(self, name, getattr(self, name) + (value or 0))


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
        llm: ModelProvider | anthropic.AsyncAnthropic,
        registry: ToolRegistry,
        ctx: ToolContext,
        config: AgentConfig,
    ) -> None:
        # An Anthropic client is accepted directly for convenience (Claude is the default).
        self._provider = llm if isinstance(llm, ModelProvider) else AnthropicProvider(llm, config)
        self._registry = registry
        self._ctx = ctx
        self._config = config
        # Built once: identical across requests so cached prefixes stay valid.
        self._tools = [*registry.definitions(), submit_report_tool()]
        self._system = system_prompt(config.max_tool_calls)

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
            provider=self._provider.name,
        )
        conversation = self._provider.start(self._system, self._tools, incident_message(incident))
        try:
            report = await self._loop(incident, conversation, state, emit)
        except InvestigationFailedError as exc:
            await emit("investigation_failed", error=str(exc))
            return result("failed", error=str(exc))
        return result("completed", report=report)

    async def _loop(
        self,
        incident: IncidentPrompt,
        conversation: Conversation,
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

            try:
                turn = await conversation.send()
            except ProviderError as exc:
                raise InvestigationFailedError(str(exc)) from exc
            state.turns += 1
            state.usage.add(turn.usage)
            await emit(
                "model_response",
                stop_reason=turn.stop_reason,
                text=turn.text,
                tool_uses=[c.name for c in turn.tool_calls],
                usage=TokenUsage().model_copy(update=dict(turn.usage)).model_dump(),
            )

            if turn.stop_reason == "refusal":
                raise InvestigationFailedError(
                    f"model declined the request (category={turn.refusal_category})"
                )
            if turn.stop_reason == "max_tokens":
                raise InvestigationFailedError("model response hit max_tokens")

            conversation.add_model_turn(turn)

            if not turn.tool_calls:
                if state.nudged:
                    raise InvestigationFailedError("model stopped without submitting a report")
                state.nudged = True
                await emit("nudge", message=NUDGE_SUBMIT)
                conversation.add_user_text(NUDGE_SUBMIT)
                continue

            results: list[ToolResultMessage] = []
            accepted: RootCauseReport | None = None
            for call in turn.tool_calls:
                if call.name == SUBMIT_REPORT:
                    outcome = await self._handle_report(call.input, incident, state, emit)
                    if isinstance(outcome, RootCauseReport):
                        accepted = outcome
                        results.append(ToolResultMessage(call, "Report accepted."))
                    else:
                        results.append(ToolResultMessage(call, outcome, is_error=True))
                else:
                    results.append(await self._handle_tool(call, state, emit))

            if accepted is not None:
                return accepted
            if state.report_failures > cfg.report_retries:
                raise InvestigationFailedError(
                    f"report failed validation {state.report_failures} times"
                )
            conversation.add_tool_results(results)

    async def _handle_tool(
        self, call: ToolCall, state: _RunState, emit: Callable[..., Awaitable[None]]
    ) -> ToolResultMessage:
        cfg = self._config
        await emit("tool_call", tool_use_id=call.id, tool=call.name, input=call.input)

        duration_ms = 0
        if state.tool_calls >= cfg.max_tool_calls:
            outcome = ToolResult(
                content=BUDGET_EXHAUSTED.format(max_tool_calls=cfg.max_tool_calls), is_error=True
            )
            executed = False
        else:
            state.tool_calls += 1
            state.called_tools.add(call.name)
            executed = True
            started = time.monotonic()
            try:
                outcome = await asyncio.wait_for(
                    asyncio.to_thread(self._registry.execute, call.name, call.input, self._ctx),
                    timeout=cfg.tool_timeout_seconds,
                )
            except TimeoutError:
                outcome = ToolResult(
                    content=f"{call.name} timed out after {cfg.tool_timeout_seconds:g}s",
                    is_error=True,
                )
            duration_ms = int((time.monotonic() - started) * 1000)

        await emit(
            "tool_result",
            tool_use_id=call.id,
            tool=call.name,
            is_error=outcome.is_error,
            executed=executed,
            duration_ms=duration_ms,
            content=outcome.content,
        )
        return ToolResultMessage(call, outcome.content, is_error=outcome.is_error)

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

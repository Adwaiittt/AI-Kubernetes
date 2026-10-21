"""Agent configuration, independent of the LLM provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.core.config import Effort, Settings

Provider = Literal["anthropic", "gemini"]


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentConfig:
    model: str
    provider: Provider = "anthropic"
    effort: Effort = "medium"  # Claude: output_config.effort; Gemini: thinking level
    max_tool_calls: int = 15
    max_turns: int = 25
    tool_timeout_seconds: float = 20.0
    token_budget: int = 600_000
    max_tokens: int = 16_000
    report_retries: int = 1
    refusal_fallback: bool = True  # Claude only

    @classmethod
    def from_settings(cls, settings: Settings) -> AgentConfig:
        if settings.llm_provider == "gemini":
            if not settings.gemini_model:
                raise ConfigurationError("GEMINI_MODEL is not set")
            model = settings.gemini_model
        else:
            if not settings.anthropic_model:
                raise ConfigurationError("ANTHROPIC_MODEL is not set")
            model = settings.anthropic_model
        return cls(
            model=model,
            provider=settings.llm_provider,
            effort=settings.anthropic_effort,
            max_tool_calls=settings.agent_max_tool_calls,
            max_turns=settings.agent_max_turns,
            tool_timeout_seconds=settings.agent_tool_timeout_seconds,
            token_budget=settings.agent_token_budget,
            max_tokens=settings.agent_max_tokens,
            report_retries=settings.agent_report_retries,
            refusal_fallback=settings.anthropic_refusal_fallback,
        )

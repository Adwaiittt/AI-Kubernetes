"""LLM providers behind one interface: Claude (default) and Gemini (LLM_PROVIDER=gemini)."""

from __future__ import annotations

from app.agent.config import AgentConfig
from app.agent.providers.base import (
    Conversation,
    ModelProvider,
    ModelTurn,
    ProviderError,
    ToolCall,
    ToolResultMessage,
)
from app.core.config import Settings


def build_provider(settings: Settings, config: AgentConfig) -> ModelProvider:
    if config.provider == "gemini":
        from app.agent.providers.gemini import GeminiProvider, build_gemini_client

        return GeminiProvider(build_gemini_client(settings), config)
    from app.agent.providers.anthropic import AnthropicProvider, build_anthropic_client

    return AnthropicProvider(build_anthropic_client(settings), config)


__all__ = [
    "Conversation",
    "ModelProvider",
    "ModelTurn",
    "ProviderError",
    "ToolCall",
    "ToolResultMessage",
    "build_provider",
]

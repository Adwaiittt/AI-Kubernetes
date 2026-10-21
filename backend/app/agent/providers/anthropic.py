"""Claude (Anthropic Messages API) adapter."""

from __future__ import annotations

from typing import Any

import anthropic

from app.agent.config import AgentConfig
from app.agent.providers.base import (
    USAGE_FIELDS,
    Conversation,
    ModelProvider,
    ModelTurn,
    ProviderError,
    ToolCall,
    ToolResultMessage,
)
from app.core.config import Settings

REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def build_anthropic_client(settings: Settings) -> anthropic.AsyncAnthropic:
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    # api_key=None lets the SDK resolve credentials from the environment itself.
    return anthropic.AsyncAnthropic(api_key=key, max_retries=3)


class AnthropicProvider(ModelProvider):
    name = "anthropic"

    def __init__(self, client: anthropic.AsyncAnthropic, config: AgentConfig) -> None:
        self._client = client
        self._config = config
        self.model = config.model

    def start(self, system: str, tools: list[dict[str, Any]], first_message: str) -> Conversation:
        return _AnthropicConversation(self._client, self._config, system, tools, first_message)


class _AnthropicConversation(Conversation):
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        config: AgentConfig,
        system: str,
        tools: list[dict[str, Any]],
        first_message: str,
    ) -> None:
        self._client = client
        self._config = config
        # Built once: byte-identical across requests so the cached prefix holds.
        self._system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        self._tools = tools
        self._messages: list[dict[str, Any]] = [{"role": "user", "content": first_message}]

    async def send(self) -> ModelTurn:
        cfg = self._config
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "system": self._system,
            "tools": self._tools,
            "messages": self._messages,
            "output_config": {"effort": cfg.effort},
            # Automatic breakpoint on the growing transcript; the explicit one on the system
            # block caches tools + system.
            "cache_control": {"type": "ephemeral"},
        }
        if cfg.refusal_fallback:
            kwargs["betas"] = [REFUSAL_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            response = await self._client.beta.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"Claude API unreachable: {exc}") from exc

        calls = [
            ToolCall(
                id=b.id,
                name=b.name,
                input=b.input if isinstance(b.input, dict) else {},
                native_id=b.id,
            )
            for b in response.content
            if b.type == "tool_use"
        ]
        stop = response.stop_reason
        details = getattr(response, "stop_details", None)
        return ModelTurn(
            stop_reason=(
                "refusal"
                if stop == "refusal"
                else "max_tokens"
                if stop == "max_tokens"
                else "tool_use"
                if calls
                else "end_turn"
            ),
            text=[b.text for b in response.content if b.type == "text" and b.text],
            tool_calls=calls,
            usage={f: getattr(response.usage, f, 0) or 0 for f in USAGE_FIELDS},
            refusal_category=getattr(details, "category", None),
            native=response.content,
        )

    def add_model_turn(self, turn: ModelTurn) -> None:
        # Passed back unchanged (keeps thinking blocks and the cache valid).
        self._messages.append({"role": "assistant", "content": turn.native})

    def add_tool_results(self, results: list[ToolResultMessage]) -> None:
        blocks = []
        for r in results:
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": r.call.native_id or r.call.id,
                "content": r.content,
            }
            if r.is_error:
                block["is_error"] = True
            blocks.append(block)
        self._messages.append({"role": "user", "content": blocks})

    def add_user_text(self, text: str) -> None:
        self._messages.append({"role": "user", "content": text})

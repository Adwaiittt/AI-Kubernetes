"""Gemini (google-genai ``generate_content``) adapter.

* Function calling is manual (automatic function calling disabled) so the shared loop keeps its
  guardrails: tool-call caps, timeouts, report validation.
* The model's turn is appended to history exactly as returned. Gemini 3 attaches thought
  signatures to it and rejects requests that drop them.
* Tool schemas are reduced to the JSON Schema subset Gemini documents (types, properties,
  required, enum, items, ...). Dropped constraints such as ``pattern`` or ``maxLength`` are still
  enforced: every tool input and the final report are validated with Pydantic, as for Claude.
"""

from __future__ import annotations

import itertools
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

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

# Role for function-result turns. The SDK's Content.role accepts only "user" or "model".
FUNCTION_RESPONSE_ROLE = "user"

_THINKING_LEVEL = {
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
    "xhigh": types.ThinkingLevel.HIGH,
    "max": types.ThinkingLevel.HIGH,
}
_REFUSALS = {
    types.FinishReason.SAFETY,
    types.FinishReason.PROHIBITED_CONTENT,
    types.FinishReason.BLOCKLIST,
    types.FinishReason.SPII,
    types.FinishReason.RECITATION,
}
_SCHEMA_KEYS = {
    "type", "properties", "required", "items", "enum", "description", "format",
    "minimum", "maximum", "minItems", "maxItems", "additionalProperties",
}  # fmt: skip


def build_gemini_client(settings: Settings) -> genai.Client:
    if settings.gemini_api_key is None:
        from app.agent.config import ConfigurationError

        raise ConfigurationError("GEMINI_API_KEY is not set")
    return genai.Client(
        api_key=settings.gemini_api_key.get_secret_value(),
        http_options=types.HttpOptions(
            timeout=120_000,  # milliseconds
            retry_options=types.HttpRetryOptions(attempts=4, http_status_codes=[429, 500, 503]),
        ),
    )


def gemini_schema(schema: Any) -> Any:
    """Reduce a Pydantic JSON schema to Gemini's documented subset (nullable via type arrays)."""
    if not isinstance(schema, dict):
        return schema
    if "anyOf" in schema:
        options = [s for s in schema["anyOf"] if s != {"type": "null"}]
        nullable = len(options) < len(schema["anyOf"])
        base = {**options[0], **{k: v for k, v in schema.items() if k == "description"}}
        merged: dict[str, Any] = gemini_schema(base)
        if nullable and isinstance(merged.get("type"), str):
            merged["type"] = [merged["type"], "null"]
        return merged
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SCHEMA_KEYS:
            continue
        if key == "properties":
            out[key] = {name: gemini_schema(sub) for name, sub in value.items()}
        elif key in ("items", "additionalProperties") and isinstance(value, dict):
            out[key] = gemini_schema(value)
        else:
            out[key] = value
    return out


class GeminiProvider(ModelProvider):
    name = "gemini"

    def __init__(self, client: genai.Client, config: AgentConfig) -> None:
        self._client = client
        self._config = config
        self.model = config.model

    def start(self, system: str, tools: list[dict[str, Any]], first_message: str) -> Conversation:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t["name"],
                            description=t["description"],
                            parameters_json_schema=gemini_schema(t["input_schema"]),
                        )
                        for t in tools
                    ]
                )
            ],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.AUTO
                )
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            max_output_tokens=self._config.max_tokens,
            thinking_config=types.ThinkingConfig(
                thinking_level=_THINKING_LEVEL[self._config.effort]
            ),
        )
        return _GeminiConversation(self._client, self._config.model, config, first_message)


class _GeminiConversation(Conversation):
    def __init__(
        self, client: genai.Client, model: str, config: types.GenerateContentConfig, first: str
    ) -> None:
        self._client = client
        self._model = model
        self._config = config
        self._contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part(text=first)])
        ]
        self._ids = itertools.count(1)

    async def send(self) -> ModelTurn:
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=self._contents, config=self._config
            )
        except errors.APIError as exc:
            raise ProviderError(f"Gemini API error {exc.code}: {exc.message}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"Gemini API unreachable: {exc}") from exc

        usage = _usage(response.usage_metadata)
        if not response.candidates:
            feedback = response.prompt_feedback
            reason = feedback.block_reason if feedback else None
            return ModelTurn(
                stop_reason="refusal",
                text=[],
                tool_calls=[],
                usage=usage,
                refusal_category=_enum_name(reason) or "blocked",
            )

        candidate = response.candidates[0]
        content = candidate.content or types.Content(role="model", parts=[])
        parts = content.parts or []
        calls = []
        for part in parts:
            fc = part.function_call
            if fc is None or not fc.name:
                continue
            calls.append(
                ToolCall(
                    id=fc.id or f"gemini_call_{next(self._ids)}",
                    name=fc.name,
                    input=dict(fc.args or {}),
                    native_id=fc.id,
                )
            )
        text = [p.text for p in parts if p.text and not p.thought]
        finish = candidate.finish_reason
        if finish in _REFUSALS:
            stop = "refusal"
        elif finish == types.FinishReason.MAX_TOKENS and not calls:
            stop = "max_tokens"
        else:
            stop = "tool_use" if calls else "end_turn"
        return ModelTurn(
            stop_reason=stop,  # type: ignore[arg-type]
            text=text,
            tool_calls=calls,
            usage=usage,
            refusal_category=_enum_name(finish) if stop == "refusal" else None,
            native=content,
        )

    def add_model_turn(self, turn: ModelTurn) -> None:
        content = turn.native
        if not isinstance(content, types.Content) or not content.parts:
            # An empty model turn can't be replayed; keep the history well-formed.
            content = types.Content(role="model", parts=[types.Part(text="(no output)")])
        self._contents.append(content)  # unchanged: carries the thought signatures

    def add_tool_results(self, results: list[ToolResultMessage]) -> None:
        parts = [
            types.Part(
                function_response=types.FunctionResponse(
                    id=r.call.native_id,
                    name=r.call.name,
                    response={"error": r.content} if r.is_error else {"output": r.content},
                )
            )
            for r in results
        ]
        self._contents.append(types.Content(role=FUNCTION_RESPONSE_ROLE, parts=parts))

    def add_user_text(self, text: str) -> None:
        self._contents.append(types.Content(role="user", parts=[types.Part(text=text)]))


def _usage(meta: types.GenerateContentResponseUsageMetadata | None) -> dict[str, int]:
    if meta is None:
        return {}
    prompt = meta.prompt_token_count or 0
    cached = meta.cached_content_token_count or 0
    return {
        "input_tokens": max(prompt - cached, 0),
        # Thinking tokens are billed at the output rate.
        "output_tokens": (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0),
        "cache_read_input_tokens": cached,  # implicit caching
        "cache_creation_input_tokens": 0,
    }


def _enum_name(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "name", value)).lower()

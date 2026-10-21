"""Provider-neutral interface between the investigation loop and an LLM API.

Each provider keeps the conversation in its *native* format (so provider-specific state such as
Claude thinking blocks or Gemini thought signatures is replayed byte-for-byte) and translates
each model turn into a :class:`ModelTurn` for the shared loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

StopReason = Literal["tool_use", "end_turn", "max_tokens", "refusal"]
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class ProviderError(RuntimeError):
    """The model API failed (HTTP error, network error). Message is safe to show."""


@dataclass(frozen=True)
class ToolCall:
    id: str  # stable id used in the transcript/UI
    name: str
    input: dict[str, Any]
    native_id: str | None = None  # the provider's own call id, if it sent one


@dataclass(frozen=True)
class ToolResultMessage:
    call: ToolCall
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ModelTurn:
    stop_reason: StopReason
    text: list[str]
    tool_calls: list[ToolCall]
    usage: Mapping[str, int] = field(default_factory=dict)
    refusal_category: str | None = None
    native: Any = None  # provider-native assistant content, appended to history unchanged


class Conversation(ABC):
    """One investigation's message history with a model. Append-only."""

    @abstractmethod
    async def send(self) -> ModelTurn: ...

    @abstractmethod
    def add_model_turn(self, turn: ModelTurn) -> None: ...

    @abstractmethod
    def add_tool_results(self, results: list[ToolResultMessage]) -> None: ...

    @abstractmethod
    def add_user_text(self, text: str) -> None: ...


class ModelProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def start(self, system: str, tools: list[dict[str, Any]], first_message: str) -> Conversation:
        """Begin a conversation. ``tools`` are generic {name, description, input_schema} dicts."""

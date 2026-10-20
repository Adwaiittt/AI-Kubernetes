"""A scripted stand-in for ``anthropic.AsyncAnthropic`` returning canned responses."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, cast

import anthropic
from anthropic.types.beta import BetaMessage

MODEL = "claude-sonnet-5-5"


class FakeMessages:
    def __init__(self, responses: list[BetaMessage | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> BetaMessage:
        # Snapshot: the loop keeps appending to the same messages list after the call.
        self.calls.append(copy.deepcopy(kwargs))
        if not self._responses:
            raise AssertionError("model called more times than scripted")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def fake_client(
    *responses: BetaMessage | Exception,
) -> tuple[anthropic.AsyncAnthropic, FakeMessages]:
    messages = FakeMessages(list(responses))
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return cast(anthropic.AsyncAnthropic, client), messages


_counter = 0


def tool_use(name: str, tool_input: dict[str, Any], tid: str | None = None) -> dict[str, Any]:
    global _counter
    _counter += 1
    return {
        "type": "tool_use",
        "id": tid or f"toolu_{_counter:04d}",
        "name": name,
        "input": tool_input,
    }


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def message(
    *blocks: dict[str, Any],
    stop_reason: str | None = None,
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cache_read: int = 0,
    cache_creation: int = 0,
    stop_details: dict[str, Any] | None = None,
) -> BetaMessage:
    if stop_reason is None:
        stop_reason = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    payload: dict[str, Any] = {
        "id": f"msg_{_counter}",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": list(blocks),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation,
        },
    }
    if stop_details is not None:
        payload["stop_details"] = stop_details
    return BetaMessage.model_validate(payload)


def report_input(
    *,
    tools: tuple[str, ...] = ("describe_pod",),
    namespace: str = "demo",
    fix: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "summary": "checkout pods are OOMKilled in a loop",
        "root_cause": "The 128Mi memory limit is below the workload's steady-state usage.",
        "confidence": "high",
        "evidence": [
            {
                "tool_name": tool,
                "excerpt": '"last_termination":{"reason":"OOMKilled","exit_code":137}',
                "why_it_matters": "Exit 137 + OOMKilled: the kernel killed it for memory.",
            }
            for tool in tools
        ],
        "proposed_fix": {
            "action_type": "patch_resources",
            "namespace": namespace,
            "target": "checkout",
            "parameters": {"container": "app", "memory_limit": "512Mi"},
            "rationale": "Raise the limit above observed peak usage.",
            "risk": "Higher memory reservation per pod; may not schedule on a full node.",
        }
        if fix
        else None,
        "alternative_hypotheses": ["A memory leak introduced in the latest image"],
    }
    report.update(overrides)
    return report

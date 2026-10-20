"""Run the real API with a *scripted* stand-in for Claude, for demos without an API key.

Everything else is real: the six read-only tools run against your cluster as monitor-reader,
results stream over SSE, and approvals go through the real executor service. Only the model's
decisions are canned (a fixed OOMKilled investigation of deploy/demo/checkout-oom.yaml).

    kubectl apply -f deploy/demo/checkout-oom.yaml
    uv run python scripts/demo_server.py          # API on :8000 (executor must run on :8001)
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from typing import Any

import uvicorn
from anthropic.types.beta import BetaMessage

from app.agent.loop import AgentConfig, Investigator
from app.agent.report import SUBMIT_REPORT
from app.agent.tools import build_registry
from app.api.deps import build_state
from app.core.config import get_settings
from app.main import create_app
from app.services.investigations import InvestigationService
from app.services.runner import TaskRunner

DEMO_MODEL = "scripted-demo (no LLM)"
STEP_DELAY = float(os.environ.get("DEMO_STEP_DELAY", "1.5"))
_ids = itertools.count(1)


def _msg(*blocks: dict[str, Any], stop: str = "tool_use") -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": f"msg_demo_{next(_ids)}",
            "type": "message",
            "role": "assistant",
            "model": DEMO_MODEL,
            "content": list(blocks),
            "stop_reason": stop,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    )


def _tool(tool: str, /, **inp: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_demo_{next(_ids)}", "name": tool, "input": inp}


def _text(t: str) -> dict[str, Any]:
    return {"type": "text", "text": t}


class ScriptedMessages:
    """Answers each turn from a fixed script, reading real tool results for the evidence."""

    async def create(self, **kwargs: Any) -> BetaMessage:
        await asyncio.sleep(STEP_DELAY)
        messages = kwargs["messages"]
        ns = "demo"
        turn = sum(1 for m in messages if m["role"] == "assistant")
        if turn == 0:
            return _msg(
                _text("Starting broad: pods and recent events in the namespace."),
                _tool("list_pods", namespace=ns, label_selector="app=checkout"),
                _tool("get_events", namespace=ns),
            )
        if turn == 1:
            pod = _first_pod(messages)
            return _msg(
                _text(f"{pod} is restarting; checking its last termination and limits."),
                _tool("describe_pod", namespace=ns, name=pod),
                _tool("get_pod_logs", namespace=ns, name=pod, previous=True, tail_lines=50),
            )
        if turn == 2:
            return _msg(
                _text("Confirming usage against limits and the rollout state."),
                _tool("get_metrics", namespace=ns),
                _tool("get_rollout_status", namespace=ns, deployment="checkout"),
            )
        return _msg(_tool(SUBMIT_REPORT, **_report(messages)))


def _results(messages: list[dict[str, Any]]) -> list[str]:
    return [
        b["content"]
        for m in messages
        if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
        if b.get("type") == "tool_result" and not b.get("is_error")
    ]


def _first_pod(messages: list[dict[str, Any]]) -> str:
    for content in _results(messages):
        try:
            pods = json.loads(content).get("pods")
        except ValueError:
            continue
        if pods:
            return str(pods[0]["name"])
    return "checkout"


def _report(messages: list[dict[str, Any]]) -> dict[str, Any]:
    describe = next((r for r in _results(messages) if '"last_termination"' in r), "")
    excerpt = describe[describe.find('"last_termination"') :][:220] or "last_termination: OOMKilled"
    return {
        "summary": "checkout is crash-looping because its container is OOMKilled at 32Mi",
        "root_cause": (
            "The app container's memory limit (32Mi) is far below what the workload needs while "
            "loading its catalog; the kernel OOM-kills it (exit 137) and the kubelet restarts it."
        ),
        "confidence": "high",
        "evidence": [
            {
                "tool_name": "describe_pod",
                "excerpt": excerpt,
                "why_it_matters": "Last termination reason OOMKilled with exit code 137 confirms "
                "a kernel OOM kill, not an application error.",
            },
            {
                "tool_name": "list_pods",
                "excerpt": "status_reason: CrashLoopBackOff, restarts > 0",
                "why_it_matters": "The pod is restarting repeatedly, matching the symptom.",
            },
        ],
        "proposed_fix": {
            "action_type": "patch_resources",
            "namespace": "demo",
            "target": "checkout",
            "parameters": {"container": "app", "memory_limit": "128Mi", "memory_request": "64Mi"},
            "rationale": "Raise the limit well above the observed working set.",
            "risk": "Each pod reserves 48Mi more memory; if usage keeps growing this is a leak, "
            "not a sizing problem, and the pod will be OOMKilled again at 128Mi.",
        },
        "alternative_hypotheses": ["Unbounded memory growth (leak) rather than under-sizing"],
    }


def main() -> None:
    settings = get_settings()
    state = build_state(settings)
    if state.reader is None:
        raise SystemExit("MONITOR_READER_KUBECONFIG is required for the demo")
    client: Any = type(
        "ScriptedClient", (), {"beta": type("B", (), {"messages": ScriptedMessages()})()}
    )()
    investigator = Investigator(
        client,
        build_registry(),
        state.reader,
        AgentConfig(model=DEMO_MODEL, refusal_fallback=False),
    )
    state.investigations = InvestigationService(state.engine, investigator, state.broker.publish)
    state.runner = TaskRunner(state.engine, state.broker, state.investigations, state.approvals)
    uvicorn.run(create_app(state), host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()

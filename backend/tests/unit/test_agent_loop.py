from __future__ import annotations

import asyncio
from typing import Any

import anthropic
import httpx2
import pytest
from kubernetes import client as k

from app.agent.loop import AgentConfig, InvestigationResult, Investigator
from app.agent.prompts import BUDGET_EXHAUSTED, NUDGE_SUBMIT, IncidentPrompt
from app.agent.report import SUBMIT_REPORT
from app.agent.tools import build_registry
from app.agent.tools.base import Tool, ToolInput, ToolOutput, ToolRegistry
from tests.conftest import FakeCluster
from tests.unit import factories as f
from tests.unit.agent_fakes import MODEL, fake_client, message, report_input, text, tool_use

INCIDENT = IncidentPrompt(namespace="demo", target="checkout", symptom="checkout keeps restarting")


def make(
    cluster: FakeCluster, *responses: Any, registry: ToolRegistry | None = None, **cfg: Any
) -> tuple[Investigator, Any]:
    client, fake = fake_client(*responses)
    config = AgentConfig(model=MODEL, **cfg)
    return Investigator(client, registry or build_registry(), cluster.ctx, config), fake


def run(inv: Investigator) -> InvestigationResult:
    return asyncio.run(inv.run(INCIDENT))


def _program_cluster(cluster: FakeCluster) -> None:
    cluster.core.list_namespaced_pod.return_value = f.pod_list(
        f.pod("checkout-7d9f-abc", statuses=[f.oom_status()])
    )
    cluster.core.list_namespaced_event.return_value = k.CoreV1EventList(
        items=[f.event("BackOff", "Back-off restarting failed container")]
    )
    cluster.core.read_namespaced_pod.return_value = f.pod(
        "checkout-7d9f-abc",
        containers=[f.container(limits={"memory": "128Mi"})],
        statuses=[f.oom_status()],
    )


# --- (a) multi-step tool use ----------------------------------------------------------------


def test_multi_step_investigation(cluster: FakeCluster) -> None:
    _program_cluster(cluster)
    lp = tool_use("list_pods", {"namespace": "demo"})
    ev = tool_use("get_events", {"namespace": "demo"})
    dp = tool_use("describe_pod", {"namespace": "demo", "name": "checkout-7d9f-abc"})
    inv, fake = make(
        cluster,
        message(text("Checking pods and events first."), lp, ev, cache_creation=3000),
        message(dp, cache_read=3000),
        message(
            tool_use(SUBMIT_REPORT, report_input(tools=("list_pods", "describe_pod"))),
            cache_read=4000,
        ),
    )
    result = run(inv)

    assert result.status == "completed", result.error
    assert result.report is not None
    assert result.report.proposed_fix is not None
    assert result.report.proposed_fix.parameters == {"container": "app", "memory_limit": "512Mi"}
    assert result.tool_calls == 3
    assert result.model_turns == 3
    assert result.usage.input_tokens == 3000
    assert result.usage.cache_read_input_tokens == 7000
    assert result.usage.cache_creation_input_tokens == 3000

    # Parallel tool calls -> one user message carrying both tool_result blocks, in order.
    second = fake.calls[1]["messages"]
    assert [b["tool_use_id"] for b in second[-1]["content"]] == [lp["id"], ev["id"]]
    assert '"checkout-7d9f-abc"' in second[-1]["content"][0]["content"]
    # The assistant turn is passed back unchanged (keeps thinking blocks / cache valid).
    assert second[-2]["role"] == "assistant"
    assert [b.type for b in second[-2]["content"]] == ["text", "tool_use", "tool_use"]

    types = [e.type for e in result.transcript]
    assert types == [
        "investigation_started",
        "model_response", "tool_call", "tool_result", "tool_call", "tool_result",
        "model_response", "tool_call", "tool_result",
        "model_response", "report_accepted",
    ]  # fmt: skip


def test_request_shape_caching_and_fallback(cluster: FakeCluster) -> None:
    _program_cluster(cluster)
    inv, fake = make(
        cluster,
        message(tool_use("describe_pod", {"namespace": "demo", "name": "checkout-7d9f-abc"})),
        message(tool_use(SUBMIT_REPORT, report_input())),
        effort="high",
    )
    run(inv)
    first, second = fake.calls
    assert first["model"] == MODEL
    assert first["output_config"] == {"effort": "high"}
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["fallbacks"] == "default"
    assert first["betas"] == ["server-side-fallback-2026-07-01"]
    assert "tool_choice" not in first  # forced tool use is rejected on current models
    names = [t["name"] for t in first["tools"]]
    assert names[-1] == SUBMIT_REPORT and len(names) == 7
    # Prefix stability: system and tools are byte-identical across requests.
    assert first["system"] == second["system"]
    assert first["tools"] == second["tools"]


def test_refusal_fallback_can_be_disabled(cluster: FakeCluster) -> None:
    _program_cluster(cluster)
    inv, fake = make(
        cluster,
        message(tool_use("describe_pod", {"namespace": "demo", "name": "checkout-7d9f-abc"})),
        message(tool_use(SUBMIT_REPORT, report_input())),
        refusal_fallback=False,
    )
    run(inv)
    assert "fallbacks" not in fake.calls[0]
    assert "betas" not in fake.calls[0]


# --- (b) tool errors ------------------------------------------------------------------------


def test_tool_error_is_returned_to_the_model(cluster: FakeCluster) -> None:
    cluster.core.read_namespaced_pod.side_effect = f.api_error(404, 'pods "ghost" not found')
    cluster.core.list_namespaced_pod.return_value = f.pod_list(
        f.pod("checkout-7d9f-abc", statuses=[f.oom_status()])
    )
    bad = tool_use("describe_pod", {"namespace": "demo", "name": "ghost"})
    inv, fake = make(
        cluster,
        message(bad),
        message(tool_use("list_pods", {"namespace": "demo"})),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",)))),
    )
    result = run(inv)

    assert result.status == "completed"
    err = fake.calls[1]["messages"][-1]["content"][0]
    assert err == {
        "type": "tool_result",
        "tool_use_id": bad["id"],
        "content": 'Kubernetes API error 404: pods "ghost" not found',
        "is_error": True,
    }
    failed = [e for e in result.transcript if e.type == "tool_result" and e.data["is_error"]]
    assert len(failed) == 1


def test_invalid_tool_input_and_unknown_tool(cluster: FakeCluster) -> None:
    _program_cluster(cluster)
    inv, fake = make(
        cluster,
        message(
            tool_use("list_pods", {"namespace": "NOT valid"}),
            tool_use("delete_pod", {"namespace": "demo", "name": "x"}),
        ),
        message(tool_use("list_pods", {"namespace": "demo"})),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",)))),
    )
    result = run(inv)
    assert result.status == "completed"
    results = fake.calls[1]["messages"][-1]["content"]
    assert all(r["is_error"] for r in results)
    assert "Unknown tool 'delete_pod'" in results[1]["content"]


class _SlowIn(ToolInput):
    namespace: str


class _SlowOut(ToolOutput):
    ok: bool


def _slow(_ctx: Any, _p: _SlowIn) -> _SlowOut:
    import time

    time.sleep(1.0)
    return _SlowOut(ok=True)


def test_tool_timeout_becomes_tool_error(cluster: FakeCluster) -> None:
    registry = ToolRegistry([Tool("list_pods", "slow tool", _SlowIn, _slow)])
    inv, fake = make(
        cluster,
        message(tool_use("list_pods", {"namespace": "demo"})),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",)))),
        registry=registry,
        tool_timeout_seconds=0.05,
    )
    result = run(inv)
    assert result.status == "completed"
    res = fake.calls[1]["messages"][-1]["content"][0]
    assert res["is_error"] is True
    assert res["content"] == "list_pods timed out after 0.05s"


# --- (c) tool-call cap ----------------------------------------------------------------------


def test_tool_call_cap_is_enforced(cluster: FakeCluster) -> None:
    _program_cluster(cluster)
    calls = [tool_use("list_pods", {"namespace": "demo"}) for _ in range(3)]
    inv, fake = make(
        cluster,
        message(calls[0], calls[1]),
        message(calls[2]),  # over the cap: must not execute
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",), confidence="low"))),
        max_tool_calls=2,
    )
    result = run(inv)

    assert result.status == "completed"
    assert result.tool_calls == 2
    assert cluster.core.list_namespaced_pod.call_count == 2
    refused = fake.calls[2]["messages"][-1]["content"][0]
    assert refused["is_error"] is True
    assert refused["content"] == BUDGET_EXHAUSTED.format(max_tool_calls=2)
    skipped = [e for e in result.transcript if e.type == "tool_result" and not e.data["executed"]]
    assert len(skipped) == 1
    # The tool list is never changed mid-conversation (that would invalidate the cache and
    # the model's thinking blocks); the cap is enforced in the results instead.
    assert fake.calls[0]["tools"] == fake.calls[2]["tools"]


def test_model_that_never_stops_hits_turn_limit(cluster: FakeCluster) -> None:
    _program_cluster(cluster)
    inv, _ = make(
        cluster,
        *[message(tool_use("list_pods", {"namespace": "demo"})) for _ in range(4)],
        max_tool_calls=2,
        max_turns=4,
    )
    result = run(inv)
    assert result.status == "failed"
    assert result.error == "stopped after 4 model turns"
    assert result.tool_calls == 2
    assert result.transcript[-1].type == "investigation_failed"


def test_token_budget_aborts(cluster: FakeCluster) -> None:
    _program_cluster(cluster)
    inv, fake = make(
        cluster,
        message(tool_use("list_pods", {"namespace": "demo"}), input_tokens=5000),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",)))),
        token_budget=4000,
    )
    result = run(inv)
    assert result.status == "failed"
    assert result.error is not None and result.error.startswith("token budget exhausted")
    assert len(fake.calls) == 1


# --- (d) malformed report, retried once -----------------------------------------------------


def test_malformed_report_is_retried_once(cluster: FakeCluster) -> None:
    _program_cluster(cluster)
    bad_submit = tool_use(SUBMIT_REPORT, report_input(confidence="very high", evidence=[]))
    inv, fake = make(
        cluster,
        message(tool_use("describe_pod", {"namespace": "demo", "name": "checkout-7d9f-abc"})),
        message(bad_submit),
        message(tool_use(SUBMIT_REPORT, report_input())),
    )
    result = run(inv)

    assert result.status == "completed"
    assert result.report is not None and result.report.confidence == "high"
    rejection = fake.calls[2]["messages"][-1]["content"][0]
    assert rejection["tool_use_id"] == bad_submit["id"]
    assert rejection["is_error"] is True
    assert "confidence" in rejection["content"]
    assert "evidence" in rejection["content"]
    assert "1 attempt(s) left" in rejection["content"]
    rejected = [e for e in result.transcript if e.type == "report_rejected"]
    assert len(rejected) == 1


def test_second_malformed_report_fails_the_investigation(cluster: FakeCluster) -> None:
    _program_cluster(cluster)
    inv, fake = make(
        cluster,
        message(tool_use("describe_pod", {"namespace": "demo", "name": "checkout-7d9f-abc"})),
        message(tool_use(SUBMIT_REPORT, {"summary": "oops"})),
        message(tool_use(SUBMIT_REPORT, {"summary": "oops again"})),
    )
    result = run(inv)
    assert result.status == "failed"
    assert result.error == "report failed validation 2 times"
    assert result.report is None
    assert len(fake.calls) == 3


def test_report_citing_uncalled_tool_is_rejected(cluster: FakeCluster) -> None:
    _program_cluster(cluster)
    inv, fake = make(
        cluster,
        message(tool_use("list_pods", {"namespace": "demo"})),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("get_metrics",)))),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",)))),
    )
    result = run(inv)
    assert result.status == "completed"
    rejection = fake.calls[2]["messages"][-1]["content"][0]["content"]
    assert "cites get_metrics, which was not called" in rejection


def test_fix_outside_investigated_namespace_is_rejected(cluster: FakeCluster) -> None:
    _program_cluster(cluster)
    inv, fake = make(
        cluster,
        message(tool_use("list_pods", {"namespace": "demo"})),
        message(
            tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",), namespace="kube-system"))
        ),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",)))),
    )
    result = run(inv)
    assert result.status == "completed"
    assert "kube-system" in fake.calls[2]["messages"][-1]["content"][0]["content"]


def test_report_without_fix_is_valid(cluster: FakeCluster) -> None:
    _program_cluster(cluster)
    inv, _ = make(
        cluster,
        message(tool_use("list_pods", {"namespace": "demo"})),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",), fix=False))),
    )
    result = run(inv)
    assert result.status == "completed"
    assert result.report is not None and result.report.proposed_fix is None


# --- other stop conditions ------------------------------------------------------------------


def test_model_that_answers_in_text_is_nudged_once(cluster: FakeCluster) -> None:
    _program_cluster(cluster)
    inv, fake = make(
        cluster,
        message(tool_use("list_pods", {"namespace": "demo"})),
        message(text("The pod is OOMKilled; raise the limit.")),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",)))),
    )
    result = run(inv)
    assert result.status == "completed"
    assert fake.calls[2]["messages"][-1] == {"role": "user", "content": NUDGE_SUBMIT}


def test_model_that_never_submits_fails_after_nudge(cluster: FakeCluster) -> None:
    inv, _ = make(cluster, message(text("I think it's DNS.")), message(text("Definitely DNS.")))
    result = run(inv)
    assert result.status == "failed"
    assert result.error == "model stopped without submitting a report"


def test_refusal_fails_cleanly(cluster: FakeCluster) -> None:
    inv, _ = make(
        cluster,
        message(
            stop_reason="refusal",
            stop_details={"type": "refusal", "category": "cyber", "explanation": None},
        ),
    )
    result = run(inv)
    assert result.status == "failed"
    assert result.error == "model declined the request (category=cyber)"
    assert result.error_kind == "agent"
    assert result.transcript[-1].data["kind"] == "agent"


def test_max_tokens_fails_cleanly(cluster: FakeCluster) -> None:
    inv, _ = make(cluster, message(text("partial"), stop_reason="max_tokens"))
    result = run(inv)
    assert result.status == "failed"
    assert result.error == "model response hit max_tokens"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            anthropic.RateLimitError(
                "rate limited",
                response=httpx2.Response(429, request=httpx2.Request("POST", "https://x")),
                body=None,
            ),
            "Claude API error 429: rate limited",
        ),
        (
            anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")),
            "Claude API unreachable: Connection error.",
        ),
    ],
)
def test_api_errors_fail_cleanly(cluster: FakeCluster, exc: Exception, expected: str) -> None:
    inv, _ = make(cluster, exc)
    result = run(inv)
    assert result.status == "failed"
    assert result.error == expected
    assert result.error_kind == "provider"  # the API failed, not the agent

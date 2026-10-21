"""Gemini adapter: real google.genai response types behind a scripted fake client."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors, types

from app.agent.config import AgentConfig, ConfigurationError
from app.agent.loop import Investigator
from app.agent.prompts import IncidentPrompt
from app.agent.providers import build_provider
from app.agent.providers.anthropic import AnthropicProvider
from app.agent.providers.gemini import FUNCTION_RESPONSE_ROLE, GeminiProvider, gemini_schema
from app.agent.report import SUBMIT_REPORT, submit_report_tool
from app.agent.tools import build_registry
from app.core.config import Settings
from tests.conftest import FakeCluster
from tests.unit import factories as f
from tests.unit.agent_fakes import report_input

MODEL = "gemini-3.8-flash"
INCIDENT = IncidentPrompt(namespace="demo", target="checkout", symptom="restarting")


class FakeModels:
    def __init__(self, responses: list[types.GenerateContentResponse | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
        # contents is a list of Content (agent loop) or a plain string (judge); both are valid.
        snapshot = (
            [c.model_copy(deep=True) for c in contents] if isinstance(contents, list) else contents
        )
        self.calls.append({"model": model, "contents": snapshot, "config": config})
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def fake_gemini(*responses: Any) -> tuple[Any, FakeModels]:
    models = FakeModels(list(responses))
    return SimpleNamespace(aio=SimpleNamespace(models=models)), models


def call(name: str, args: dict[str, Any], cid: str | None = "c1", sig: bytes | None = None) -> Any:
    return types.Part(
        function_call=types.FunctionCall(id=cid, name=name, args=args), thought_signature=sig
    )


def response(
    *parts: types.Part,
    finish: types.FinishReason = types.FinishReason.STOP,
    prompt: int = 1000,
    cached: int = 0,
    out: int = 50,
    thoughts: int = 20,
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=list(parts)), finish_reason=finish
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt,
            cached_content_token_count=cached,
            candidates_token_count=out,
            thoughts_token_count=thoughts,
        ),
    )


def investigator(cluster: FakeCluster, *responses: Any, **cfg: Any) -> tuple[Investigator, Any]:
    client, models = fake_gemini(*responses)
    config = AgentConfig(model=MODEL, provider="gemini", **cfg)
    return Investigator(
        GeminiProvider(client, config), build_registry(), cluster.ctx, config
    ), models


def run(inv: Investigator) -> Any:
    return asyncio.run(inv.run(INCIDENT))


@pytest.fixture(autouse=True)
def _pods(cluster: FakeCluster) -> None:
    cluster.core.list_namespaced_pod.return_value = f.pod_list(f.pod("checkout-7d9f"))


def test_multi_step_investigation_with_gemini(cluster: FakeCluster) -> None:
    inv, models = investigator(
        cluster,
        response(
            types.Part(text="Looking at the pods first."),
            call("list_pods", {"namespace": "demo"}, sig=b"thought-sig-1"),
            cached=0,
        ),
        response(
            call(SUBMIT_REPORT, report_input(tools=("list_pods",)), cid="c2"),
            prompt=3000,
            cached=900,
        ),
    )
    result = run(inv)

    assert result.status == "completed", result.error
    assert result.report is not None and result.report.proposed_fix is not None
    assert result.tool_calls == 1 and result.model_turns == 2
    # input excludes implicitly-cached tokens; thinking counts as output
    assert result.usage.input_tokens == 1000 + 2100
    assert result.usage.cache_read_input_tokens == 900
    assert result.usage.output_tokens == 2 * (50 + 20)

    second = models.calls[1]["contents"]
    assert [c.role for c in second] == ["user", "model", FUNCTION_RESPONSE_ROLE]
    # The model's turn is replayed unchanged, thought signature included (Gemini 3 requires it).
    assert second[1].parts[1].thought_signature == b"thought-sig-1"
    assert second[1].parts[0].text == "Looking at the pods first."
    fr = second[2].parts[0].function_response
    assert (fr.id, fr.name) == ("c1", "list_pods")
    assert "checkout-7d9f" in fr.response["output"]

    tool_call = next(e for e in result.transcript if e.type == "tool_call")
    assert tool_call.data == {"tool_use_id": "c1", "tool": "list_pods",
                              "input": {"namespace": "demo"}}  # fmt: skip
    started = result.transcript[0]
    assert started.data["provider"] == "gemini" and started.data["model"] == MODEL


def test_request_config(cluster: FakeCluster) -> None:
    inv, models = investigator(
        cluster,
        response(call(SUBMIT_REPORT, report_input(tools=("list_pods",)))),
        response(call(SUBMIT_REPORT, report_input(tools=("list_pods",)))),
        effort="high",
    )
    run(inv)
    cfg: types.GenerateContentConfig = models.calls[0]["config"]
    assert models.calls[0]["model"] == MODEL
    assert cfg.automatic_function_calling is not None and cfg.automatic_function_calling.disable
    assert cfg.tool_config is not None and cfg.tool_config.function_calling_config is not None
    assert cfg.tool_config.function_calling_config.mode == types.FunctionCallingConfigMode.AUTO
    assert cfg.thinking_config is not None
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.HIGH
    assert "senior site reliability engineer" in str(cfg.system_instruction)
    assert cfg.tools is not None
    names = [fd.name for fd in cfg.tools[0].function_declarations or []]  # type: ignore[union-attr]
    assert names[-1] == SUBMIT_REPORT and len(names) == 7
    # The config object is reused verbatim across turns (prefix stays identical).
    assert models.calls[1]["config"] is cfg


def test_tool_errors_and_parallel_calls(cluster: FakeCluster) -> None:
    cluster.core.read_namespaced_pod.side_effect = f.api_error(404, 'pods "ghost" not found')
    inv, models = investigator(
        cluster,
        response(
            call("describe_pod", {"namespace": "demo", "name": "ghost"}, cid="a"),
            call("list_pods", {"namespace": "demo"}, cid="b"),
        ),
        response(call(SUBMIT_REPORT, report_input(tools=("list_pods",)), cid="c")),
    )
    result = run(inv)
    assert result.status == "completed"
    reply = models.calls[1]["contents"][-1]
    assert [p.function_response.id for p in reply.parts] == ["a", "b"]
    assert reply.parts[0].function_response.response == {
        "error": 'Kubernetes API error 404: pods "ghost" not found'
    }
    assert "output" in reply.parts[1].function_response.response


def test_calls_without_ids_get_stable_local_ids(cluster: FakeCluster) -> None:
    inv, models = investigator(
        cluster,
        response(call("list_pods", {"namespace": "demo"}, cid=None)),
        response(call(SUBMIT_REPORT, report_input(tools=("list_pods",)), cid=None)),
    )
    result = run(inv)
    assert result.status == "completed"
    tool_call = next(e for e in result.transcript if e.type == "tool_call")
    assert tool_call.data["tool_use_id"] == "gemini_call_1"
    fr = models.calls[1]["contents"][-1].parts[0].function_response
    assert fr.id is None and fr.name == "list_pods"  # no invented id sent to the API


def test_text_only_turn_is_nudged(cluster: FakeCluster) -> None:
    inv, models = investigator(
        cluster,
        response(types.Part(text="It's probably memory.")),
        response(call("list_pods", {"namespace": "demo"})),
        response(call(SUBMIT_REPORT, report_input(tools=("list_pods",)), cid="c2")),
    )
    result = run(inv)
    assert result.status == "completed"
    nudge = models.calls[1]["contents"][-1]
    assert nudge.role == "user" and "submit_report" in (nudge.parts[0].text or "")


def test_thought_parts_are_not_reported_as_text(cluster: FakeCluster) -> None:
    inv, _ = investigator(
        cluster,
        response(types.Part(text="internal reasoning", thought=True),
                 call("list_pods", {"namespace": "demo"})),
        response(call(SUBMIT_REPORT, report_input(tools=("list_pods",)), cid="c2")),
    )  # fmt: skip
    result = run(inv)
    first = next(e for e in result.transcript if e.type == "model_response")
    assert first.data["text"] == []


def test_safety_refusal(cluster: FakeCluster) -> None:
    inv, _ = investigator(cluster, response(finish=types.FinishReason.SAFETY))
    result = run(inv)
    assert result.status == "failed"
    assert result.error == "model declined the request (category=safety)"


def test_blocked_prompt(cluster: FakeCluster) -> None:
    blocked = types.GenerateContentResponse(
        candidates=[],
        prompt_feedback=types.GenerateContentResponsePromptFeedback(
            block_reason=types.BlockedReason.PROHIBITED_CONTENT
        ),
    )
    inv, _ = investigator(cluster, blocked)
    result = run(inv)
    assert result.error == "model declined the request (category=prohibited_content)"


def test_max_tokens(cluster: FakeCluster) -> None:
    inv, _ = investigator(
        cluster, response(types.Part(text="partial"), finish=types.FinishReason.MAX_TOKENS)
    )
    assert run(inv).error == "model response hit max_tokens"


def test_api_error_fails_cleanly(cluster: FakeCluster) -> None:
    err = errors.ClientError(
        429, {"error": {"code": 429, "message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}}
    )
    inv, _ = investigator(cluster, err)
    result = run(inv)
    assert result.status == "failed"
    assert result.error is not None and result.error.startswith("Gemini API error 429")
    assert "quota exceeded" in result.error


def test_report_validation_still_applies(cluster: FakeCluster) -> None:
    inv, models = investigator(
        cluster,
        response(call("list_pods", {"namespace": "demo"})),
        response(call(SUBMIT_REPORT, report_input(tools=("list_pods",), confidence="sure"),
                      cid="c2")),
        response(call(SUBMIT_REPORT, report_input(tools=("list_pods",)), cid="c3")),
    )  # fmt: skip
    result = run(inv)
    assert result.status == "completed"
    rejection = models.calls[2]["contents"][-1].parts[0].function_response
    assert "confidence" in rejection.response["error"]


# --- schema conversion ----------------------------------------------------------------------

FORBIDDEN = {"anyOf", "pattern", "maxLength", "minLength", "default", "title", "$defs", "$ref"}


def _keys(schema: Any) -> set[str]:
    if isinstance(schema, dict):
        out = set(schema)
        for k, v in schema.items():
            if k == "properties":
                for sub in v.values():
                    out |= _keys(sub)
            elif isinstance(v, (dict, list)):
                out |= _keys(v)
        return out
    if isinstance(schema, list):
        return set().union(*(_keys(s) for s in schema)) if schema else set()
    return set()


@pytest.mark.parametrize(
    "tool", [*build_registry().definitions(), submit_report_tool()], ids=lambda t: t["name"]
)
def test_tool_schemas_use_only_gemini_supported_keywords(tool: dict[str, Any]) -> None:
    converted = gemini_schema(tool["input_schema"])
    assert not _keys(converted) & FORBIDDEN
    assert converted["type"] == "object"


def test_nullable_fields_become_type_arrays() -> None:
    pods = gemini_schema(build_registry().get("list_pods").definition()["input_schema"])
    assert pods["properties"]["label_selector"]["type"] == ["string", "null"]
    assert pods["required"] == ["namespace"]
    report = gemini_schema(submit_report_tool()["input_schema"])
    fix = report["properties"]["proposed_fix"]
    assert fix["type"] == ["object", "null"]
    assert fix["properties"]["action_type"]["enum"] == [
        "patch_resources", "rollout_restart", "rollout_undo", "scale",
    ]  # fmt: skip
    assert report["properties"]["evidence"]["items"]["type"] == "object"


# --- configuration --------------------------------------------------------------------------


def test_provider_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    settings = Settings(_env_file=None)
    config = AgentConfig.from_settings(settings)  # ANTHROPIC_MODEL not needed for Gemini
    assert (config.provider, config.model) == ("gemini", "gemini-3.8-flash")
    with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
        build_provider(settings, config)

    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    settings = Settings(_env_file=None)
    config = AgentConfig.from_settings(settings)
    provider = build_provider(settings, config)
    assert isinstance(provider, GeminiProvider) and provider.model == "gemini-3.5-flash-lite"
    assert "test-gemini-key" not in repr(settings)

    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    settings = Settings(_env_file=None)
    assert isinstance(build_provider(settings, AgentConfig.from_settings(settings)),
                      AnthropicProvider)  # fmt: skip


def test_gemini_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    from chaos import judge as judge_mod
    from chaos.scenario import load_scenarios

    verdict = response(types.Part(text='{"root_cause_correct": true, "reasoning": "same cause"}'))
    client, models = fake_gemini(verdict)
    monkeypatch.setattr("app.agent.providers.gemini.build_gemini_client", lambda _s: client)
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    judge = judge_mod.build_judge(Settings(_env_file=None), MODEL)
    truth = load_scenarios(only=["01"])[0].ground_truth

    async def grade() -> tuple[bool, str]:
        return await judge(truth, {"summary": "s", "root_cause": "r"})

    assert asyncio.run(grade()) == (True, "same cause")
    cfg = models.calls[0]["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == judge_mod.JUDGE_SCHEMA

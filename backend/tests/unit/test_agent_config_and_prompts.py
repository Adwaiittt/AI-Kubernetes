import json

import anthropic
import pytest

from app.agent.loop import AgentConfig, ConfigurationError, build_anthropic_client
from app.agent.prompts import IncidentPrompt, incident_message, system_prompt
from app.agent.report import SUBMIT_REPORT, RootCauseReport, submit_report_tool
from app.core.config import Settings


def test_config_requires_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    with pytest.raises(ConfigurationError, match="ANTHROPIC_MODEL"):
        AgentConfig.from_settings(Settings(_env_file=None))


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("ANTHROPIC_EFFORT", "high")
    monkeypatch.setenv("AGENT_MAX_TOOL_CALLS", "7")
    cfg = AgentConfig.from_settings(Settings(_env_file=None))
    assert cfg.model == "claude-sonnet-5-5"
    assert cfg.effort == "high"
    assert cfg.max_tool_calls == 7
    assert cfg.refusal_fallback is True


def test_api_key_is_secret_and_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-123")
    settings = Settings(_env_file=None)
    assert "sk-ant-test-123" not in repr(settings)
    client = build_anthropic_client(settings)
    assert isinstance(client, anthropic.AsyncAnthropic)
    assert client.api_key == "sk-ant-test-123"


def test_system_prompt_is_static_and_mentions_budget() -> None:
    assert system_prompt(15) == system_prompt(15)
    assert "budget of 15 tool calls" in system_prompt(15)
    assert "untrusted data" in system_prompt(15)


def test_incident_message() -> None:
    msg = incident_message(IncidentPrompt(namespace="demo", symptom="503s on /checkout"))
    assert "<namespace>demo</namespace>" in msg
    assert "not specified" in msg
    assert "503s on /checkout" in msg


def test_submit_report_schema_is_self_contained() -> None:
    tool = submit_report_tool()
    assert tool["name"] == SUBMIT_REPORT
    dumped = json.dumps(tool["input_schema"])
    assert "$ref" not in dumped and "$defs" not in dumped
    props = tool["input_schema"]["properties"]
    assert set(props) == set(RootCauseReport.model_fields)
    action_types = props["proposed_fix"]["anyOf"][0]["properties"]["action_type"]["enum"]
    assert action_types == ["patch_resources", "rollout_restart", "rollout_undo", "scale"]

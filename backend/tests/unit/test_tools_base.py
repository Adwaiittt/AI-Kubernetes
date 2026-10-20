from typing import Any

import pytest

from app.agent.tools import READ_ONLY_TOOLS, build_registry
from app.agent.tools.base import (
    Tool,
    ToolInput,
    ToolOutput,
    ToolRegistry,
    cap_bytes,
    cap_list,
    truncate_tail,
)
from tests.conftest import FakeCluster


def test_registry_exposes_exactly_the_six_tools() -> None:
    assert build_registry().names == [
        "list_pods",
        "describe_pod",
        "get_pod_logs",
        "get_events",
        "get_rollout_status",
        "get_metrics",
    ]


@pytest.mark.parametrize("tool", READ_ONLY_TOOLS, ids=lambda t: t.name)
def test_definitions_are_valid_claude_tool_schemas(tool: Tool) -> None:
    d = tool.definition()
    assert set(d) == {"name", "description", "input_schema"}
    assert len(d["description"]) > 40
    schema = d["input_schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "namespace" in schema["required"]
    assert "title" not in schema
    for prop in schema["properties"].values():
        assert "title" not in prop


def test_unknown_tool(cluster: FakeCluster) -> None:
    result = build_registry().execute("delete_pod", {}, cluster.ctx)
    assert result.is_error
    assert "Unknown tool 'delete_pod'" in result.content


def test_invalid_input_is_reported_not_raised(cluster: FakeCluster) -> None:
    result = build_registry().execute(
        "list_pods", {"namespace": "Not_Valid!", "extra": 1}, cluster.ctx
    )
    assert result.is_error
    assert "namespace" in result.content
    assert "extra" in result.content
    cluster.core.list_namespaced_pod.assert_not_called()


def test_unexpected_exception_becomes_tool_error(cluster: FakeCluster) -> None:
    cluster.core.list_namespaced_pod.side_effect = TimeoutError("read timed out")
    result = build_registry().execute("list_pods", {"namespace": "demo"}, cluster.ctx)
    assert result.is_error
    assert result.content == "list_pods failed: TimeoutError: read timed out"


def test_output_is_capped(cluster: FakeCluster) -> None:
    class In(ToolInput):
        namespace: str

    class Out(ToolOutput):
        blob: str

    def big(_ctx: Any, _p: In) -> Out:
        return Out(blob="y" * 10_000)

    cluster.ctx.settings.tool_output_max_bytes = 500
    registry = ToolRegistry([Tool("big", "d", In, big)])
    result = registry.execute("big", {"namespace": "demo"}, cluster.ctx)
    assert not result.is_error
    assert len(result.content) < 600
    assert "[output truncated:" in result.content


def test_duplicate_tool_names_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        ToolRegistry([READ_ONLY_TOOLS[0], READ_ONLY_TOOLS[0]])


class TestTruncateTail:
    def test_no_truncation(self) -> None:
        r = truncate_tail("a\nb\nc", max_lines=10, max_bytes=100)
        assert r.text == "a\nb\nc"
        assert not r.truncated

    def test_line_cap_keeps_tail(self) -> None:
        r = truncate_tail("\n".join(str(i) for i in range(10)), max_lines=3, max_bytes=100)
        assert r.text == "[truncated: 7 earlier lines omitted]\n7\n8\n9"
        assert r.omitted_lines == 7

    def test_byte_cap(self) -> None:
        r = truncate_tail("aaaa\nbbbb\ncccc", max_lines=10, max_bytes=10)
        assert r.text.endswith("bbbb\ncccc")
        assert r.omitted_lines == 1

    def test_single_huge_line(self) -> None:
        r = truncate_tail("short\n" + "z" * 1000, max_lines=10, max_bytes=50)
        assert r.truncated
        assert r.text.endswith("z" * 50)
        assert r.omitted_lines == 1

    def test_empty(self) -> None:
        assert truncate_tail("", 10, 10).text == ""


def test_cap_list_and_cap_bytes() -> None:
    assert cap_list([1, 2, 3], 2) == ([1, 2], 1)
    assert cap_list([1], 5) == ([1], 0)
    assert cap_bytes("short", 100) == "short"
    assert cap_bytes("x" * 20, 10).startswith("x" * 10 + "\n[output truncated: 10 bytes")

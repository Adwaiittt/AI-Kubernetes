import pytest
from pydantic import ValidationError

from app.agent.tools import build_registry
from app.agent.tools.logs import GetPodLogsInput, get_pod_logs
from tests.conftest import FakeCluster
from tests.unit import factories as f


def test_previous_logs_are_requested(cluster: FakeCluster) -> None:
    cluster.core.read_namespaced_pod_log.return_value = "starting\nFATAL: DATABASE_URL is not set\n"
    out = get_pod_logs(
        cluster.ctx,
        GetPodLogsInput(namespace="demo", name="api-1", container="app", previous=True),
    )
    cluster.core.read_namespaced_pod_log.assert_called_once_with(
        "api-1", "demo", tail_lines=200, previous=True, container="app", _request_timeout=5.0
    )
    assert out.previous is True
    assert "DATABASE_URL is not set" in out.logs
    assert out.truncated is False
    assert out.line_count == 2


def test_logs_are_truncated_to_byte_budget_keeping_the_tail(cluster: FakeCluster) -> None:
    cluster.ctx.settings.log_max_bytes = 200
    lines = [f"line {i:04d} " + "x" * 30 for i in range(100)]
    cluster.core.read_namespaced_pod_log.return_value = "\n".join(lines)
    out = get_pod_logs(cluster.ctx, GetPodLogsInput(namespace="demo", name="api-1"))
    assert out.truncated
    assert out.logs.startswith("[truncated:")
    assert out.logs.endswith(lines[-1])
    assert len(out.logs.encode()) < 300


def test_tail_lines_is_bounded() -> None:
    with pytest.raises(ValidationError):
        GetPodLogsInput(namespace="demo", name="api-1", tail_lines=100_000)


def test_empty_logs(cluster: FakeCluster) -> None:
    cluster.core.read_namespaced_pod_log.return_value = ""
    out = get_pod_logs(cluster.ctx, GetPodLogsInput(namespace="demo", name="api-1"))
    assert out.logs == "<no log output>"


def test_kubelet_unavailable_message_is_not_passed_off_as_logs(cluster: FakeCluster) -> None:
    cluster.core.read_namespaced_pod_log.return_value = (
        "unable to retrieve container logs for containerd://fd4907079436"
    )
    out = get_pod_logs(cluster.ctx, GetPodLogsInput(namespace="demo", name="api-1", previous=True))
    assert out.available is False
    assert out.logs == ""
    assert out.note is not None and "NOT application output" in out.note


def test_missing_previous_container_is_a_tool_error(cluster: FakeCluster) -> None:
    cluster.core.read_namespaced_pod_log.side_effect = f.api_error(
        400, 'previous terminated container "app" in pod "api-1" not found', "Bad Request"
    )
    result = build_registry().execute(
        "get_pod_logs", {"namespace": "demo", "name": "api-1", "previous": True}, cluster.ctx
    )
    assert result.is_error
    assert "previous terminated container" in result.content

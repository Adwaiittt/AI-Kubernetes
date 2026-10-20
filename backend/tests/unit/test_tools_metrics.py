from app.agent.tools import build_registry
from app.agent.tools.metrics import GetMetricsInput, get_metrics
from tests.conftest import FakeCluster
from tests.unit import factories as f


def _pod_metrics(name: str, cpu: str, memory: str) -> dict[str, object]:
    return {
        "metadata": {"name": name, "namespace": "demo"},
        "window": "15s",
        "containers": [{"name": "app", "usage": {"cpu": cpu, "memory": memory}}],
    }


def test_metrics_shown_next_to_limits(cluster: FakeCluster) -> None:
    cluster.custom.list_namespaced_custom_object.return_value = {
        "items": [_pod_metrics("api-1", "250000000n", "120Mi")]
    }
    cluster.core.list_namespaced_pod.return_value = f.pod_list(
        f.pod(
            "api-1",
            containers=[
                f.container(
                    requests={"cpu": "100m", "memory": "64Mi"},
                    limits={"cpu": "500m", "memory": "128Mi"},
                )
            ],
        )
    )
    out = get_metrics(cluster.ctx, GetMetricsInput(namespace="demo"))

    assert out.available
    usage = out.pods[0].containers[0]
    assert usage.cpu_usage_millicores == 250.0
    assert usage.cpu_limit_millicores == 500.0
    assert usage.cpu_pct_of_limit == 50.0
    assert usage.memory_usage_mib == 120.0
    assert usage.memory_request_mib == 64.0
    assert usage.memory_limit_mib == 128.0
    assert usage.memory_pct_of_limit == 93.8


def test_single_pod_metrics(cluster: FakeCluster) -> None:
    cluster.custom.get_namespaced_custom_object.return_value = _pod_metrics("api-1", "5m", "10Mi")
    cluster.core.read_namespaced_pod.return_value = f.pod("api-1")  # no limits set
    out = get_metrics(cluster.ctx, GetMetricsInput(namespace="demo", pod="api-1"))
    usage = out.pods[0].containers[0]
    assert usage.cpu_usage_millicores == 5.0
    assert usage.memory_limit_mib is None
    assert usage.memory_pct_of_limit is None


def test_metrics_server_absent_degrades_gracefully(cluster: FakeCluster) -> None:
    cluster.custom.list_namespaced_custom_object.side_effect = f.api_error(
        404, "the server could not find the requested resource", "Not Found"
    )
    out = get_metrics(cluster.ctx, GetMetricsInput(namespace="demo"))
    assert out.available is False
    assert out.message is not None and "metrics-server is not installed" in out.message
    # And it is a normal (non-error) tool result, so the agent can carry on.
    result = build_registry().execute("get_metrics", {"namespace": "demo"}, cluster.ctx)
    assert not result.is_error
    assert '"available":false' in result.content


def test_metrics_server_unavailable_503(cluster: FakeCluster) -> None:
    cluster.custom.list_namespaced_custom_object.side_effect = f.api_error(
        503, "the server is currently unable to handle the request", "Service Unavailable"
    )
    assert get_metrics(cluster.ctx, GetMetricsInput(namespace="demo")).available is False


def test_pod_metrics_not_yet_available(cluster: FakeCluster) -> None:
    cluster.custom.get_namespaced_custom_object.side_effect = f.api_error(
        404, 'podmetrics.metrics.k8s.io "api-1" not found', "Not Found"
    )
    out = get_metrics(cluster.ctx, GetMetricsInput(namespace="demo", pod="api-1"))
    assert out.available is True
    assert out.pods == []


def test_other_api_errors_propagate_as_tool_errors(cluster: FakeCluster) -> None:
    cluster.custom.list_namespaced_custom_object.side_effect = f.api_error(
        403, "forbidden", "Forbidden"
    )
    result = build_registry().execute("get_metrics", {"namespace": "demo"}, cluster.ctx)
    assert result.is_error
    assert "403" in result.content

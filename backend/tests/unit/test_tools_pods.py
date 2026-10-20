from kubernetes import client as k

from app.agent.tools import build_registry
from app.agent.tools.pods import DescribePodInput, ListPodsInput, describe_pod, list_pods
from tests.conftest import FakeCluster
from tests.unit import factories as f


def test_list_pods_summarizes_status(cluster: FakeCluster) -> None:
    cluster.core.list_namespaced_pod.return_value = f.pod_list(
        f.pod("checkout-b", statuses=[f.oom_status(restarts=4)]),
        f.pod("checkout-a", statuses=[f.running_status()]),
        f.pod(
            "big-job",
            phase="Pending",
            node=None,
            conditions=[
                k.V1PodCondition(type="PodScheduled", status="False", reason="Unschedulable")
            ],
        ),
    )
    out = list_pods(cluster.ctx, ListPodsInput(namespace="demo", label_selector="app=checkout"))

    cluster.core.list_namespaced_pod.assert_called_once_with(
        "demo", label_selector="app=checkout", _request_timeout=5.0
    )
    by_name = {p.name: p for p in out.pods}
    assert [p.name for p in out.pods] == ["big-job", "checkout-a", "checkout-b"]
    assert by_name["checkout-b"].restarts == 4
    assert by_name["checkout-b"].ready == "0/1"
    assert by_name["checkout-b"].status_reason == "CrashLoopBackOff"
    assert by_name["checkout-a"].ready == "1/1"
    assert by_name["checkout-a"].status_reason is None
    assert by_name["big-job"].status_reason == "Unschedulable"
    assert by_name["big-job"].node is None


def test_list_pods_caps_items(cluster: FakeCluster) -> None:
    cluster.ctx.settings.list_max_items = 2
    cluster.core.list_namespaced_pod.return_value = f.pod_list(*(f.pod(f"p{i}") for i in range(5)))
    out = list_pods(cluster.ctx, ListPodsInput(namespace="demo"))
    assert len(out.pods) == 2
    assert out.omitted == 3


def test_describe_pod_reports_oomkilled_and_resources(cluster: FakeCluster) -> None:
    probe = k.V1Probe(
        http_get=k.V1HTTPGetAction(path="/healthz", port=8080),
        initial_delay_seconds=5,
        period_seconds=10,
        failure_threshold=3,
    )
    secret_env = k.V1EnvVar(
        name="DB_PASSWORD",
        value_from=k.V1EnvVarSource(secret_key_ref=k.V1SecretKeySelector(name="db", key="pw")),
    )
    cluster.core.read_namespaced_pod.return_value = f.pod(
        "checkout-b",
        containers=[
            f.container(
                requests={"memory": "64Mi", "cpu": "100m"},
                limits={"memory": "128Mi"},
                liveness=probe,
                env=[k.V1EnvVar(name="MODE", value="prod"), secret_env],
            )
        ],
        statuses=[f.oom_status()],
    )
    out = describe_pod(cluster.ctx, DescribePodInput(namespace="demo", name="checkout-b"))

    c = out.containers[0]
    assert c.last_termination is not None
    assert c.last_termination.reason == "OOMKilled"
    assert c.last_termination.exit_code == 137
    assert c.state is not None and c.state.reason == "CrashLoopBackOff"
    assert c.resources == {
        "requests": {"memory": "64Mi", "cpu": "100m"},
        "limits": {"memory": "128Mi"},
    }
    assert c.liveness_probe is not None
    assert c.liveness_probe.type == "httpGet"
    assert c.liveness_probe.detail == "HTTP :8080/healthz"
    assert c.env == {"MODE": "literal", "DB_PASSWORD": "secretKeyRef:db"}
    assert out.owner == "ReplicaSet/checkout-7d9f"
    # The literal env *value* must never leak into tool output.
    assert "prod" not in out.model_dump_json()


def test_describe_pod_not_found_becomes_tool_error(cluster: FakeCluster) -> None:
    cluster.core.read_namespaced_pod.side_effect = f.api_error(404, 'pods "nope" not found')
    result = build_registry().execute(
        "describe_pod", {"namespace": "demo", "name": "nope"}, cluster.ctx
    )
    assert result.is_error
    assert result.content == 'Kubernetes API error 404: pods "nope" not found'

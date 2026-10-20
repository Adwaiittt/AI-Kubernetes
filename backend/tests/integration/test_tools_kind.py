"""End-to-end check of the read-only tools against a real kind cluster.

Prereqs (see deploy/scripts/setup-kind.sh):
  * kind cluster with deploy/k8s/00-namespaces.yaml + 10-reader-rbac.yaml applied
  * MONITOR_READER_KUBECONFIG pointing at the monitor-reader kubeconfig
  * the default kubeconfig context is an admin of that cluster (used only for test setup)

Run with:  uv run pytest -m integration
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator

import pytest
from kubernetes import client, config

from app.agent.tools import build_registry
from app.agent.tools.base import ToolContext
from app.core.config import Settings
from app.core.k8s import ReadOnlyClients, build_api_client

pytestmark = pytest.mark.integration

NAMESPACE = "demo"
READER_KUBECONFIG = os.environ.get("MONITOR_READER_KUBECONFIG")


@pytest.fixture(scope="module")
def admin() -> client.CoreV1Api:
    if not READER_KUBECONFIG:
        pytest.skip("MONITOR_READER_KUBECONFIG not set")
    return client.CoreV1Api(build_api_client_default())


def build_api_client_default() -> client.ApiClient:
    """Admin client from the default kubeconfig context; used only to set up fixtures."""
    loader: Callable[..., client.ApiClient] = config.new_client_from_config
    return loader()


@pytest.fixture(scope="module")
def reader_api() -> client.ApiClient:
    assert READER_KUBECONFIG
    return build_api_client(READER_KUBECONFIG, None)


@pytest.fixture(scope="module")
def ctx(reader_api: client.ApiClient) -> ToolContext:
    settings = Settings(_env_file=None)
    return ToolContext(ReadOnlyClients.from_api_client(reader_api, 15), settings)


@pytest.fixture(scope="module")
def crashing_pod(admin: client.CoreV1Api) -> Iterator[str]:
    """A pod that prints a marker and exits 1, so it restarts into CrashLoopBackOff."""
    name = f"it-crash-{uuid.uuid4().hex[:6]}"
    marker = f"FATAL boom {name}"
    admin.create_namespaced_pod(
        NAMESPACE,
        client.V1Pod(
            metadata=client.V1ObjectMeta(name=name, labels={"it": "crash"}),
            spec=client.V1PodSpec(
                restart_policy="Always",
                containers=[
                    client.V1Container(
                        name="app",
                        image="busybox:1.36",
                        # Sleep so each instance lives long enough for the kubelet to keep its log.
                        command=["sh", "-c", f"echo '{marker}'; sleep 3; exit 1"],
                        resources=client.V1ResourceRequirements(
                            requests={"cpu": "10m", "memory": "16Mi"},
                            limits={"memory": "32Mi"},
                        ),
                    )
                ],
            ),
        ),
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        pod = admin.read_namespaced_pod(name, NAMESPACE)
        statuses = (pod.status.container_statuses if pod.status else None) or []
        last = statuses[0].last_state if statuses else None
        if statuses and statuses[0].restart_count >= 1 and last and last.terminated:
            break
        time.sleep(3)
    else:
        pytest.fail(f"{name} never restarted")
    yield name
    admin.delete_namespaced_pod(name, NAMESPACE, grace_period_seconds=0)


def test_tools_against_kind(ctx: ToolContext, crashing_pod: str) -> None:
    registry = build_registry()

    pods = registry.execute(
        "list_pods", {"namespace": NAMESPACE, "label_selector": "it=crash"}, ctx
    )
    assert not pods.is_error, pods.content
    assert crashing_pod in pods.content

    described = registry.execute(
        "describe_pod", {"namespace": NAMESPACE, "name": crashing_pod}, ctx
    )
    assert not described.is_error, described.content
    assert '"exit_code":1' in described.content
    assert '"limits":{"memory":"32Mi"}' in described.content

    # The previous instance's log can briefly be unavailable while the kubelet rotates containers;
    # the tool reports that as available=false, so retry a few times.
    for _ in range(10):
        logs = registry.execute(
            "get_pod_logs", {"namespace": NAMESPACE, "name": crashing_pod, "previous": True}, ctx
        )
        assert not logs.is_error, logs.content
        if '"available":true' in logs.content:
            break
        time.sleep(2)
    assert f"FATAL boom {crashing_pod}" in logs.content

    events = registry.execute(
        "get_events", {"namespace": NAMESPACE, "involved_object": crashing_pod}, ctx
    )
    assert not events.is_error, events.content
    assert "BackOff" in events.content or "Started" in events.content

    metrics = registry.execute("get_metrics", {"namespace": NAMESPACE}, ctx)
    assert not metrics.is_error, metrics.content  # degraded is fine, an error is not


@pytest.mark.parametrize(
    ("verb", "resource", "group"),
    [
        ("delete", "pods", ""),
        ("create", "pods", ""),
        ("patch", "deployments", "apps"),
        ("get", "secrets", ""),
        ("create", "pods/exec", ""),
    ],
)
def test_reader_identity_is_denied_writes(
    reader_api: client.ApiClient, verb: str, resource: str, group: str
) -> None:
    sub = None
    if "/" in resource:
        resource, sub = resource.split("/")
    review = client.AuthorizationV1Api(reader_api).create_self_subject_access_review(
        client.V1SelfSubjectAccessReview(
            spec=client.V1SelfSubjectAccessReviewSpec(
                resource_attributes=client.V1ResourceAttributes(
                    namespace=NAMESPACE,
                    verb=verb,
                    resource=resource,
                    group=group,
                    subresource=sub,
                )
            )
        )
    )
    assert review.status is not None
    assert review.status.allowed is False

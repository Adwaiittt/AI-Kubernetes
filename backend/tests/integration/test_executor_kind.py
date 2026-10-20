"""The executor against a real kind cluster, running as the monitor-executor ServiceAccount.

Prereqs: deploy/scripts/setup-kind.sh, then
  MONITOR_READER_KUBECONFIG=.kube/monitor-reader.kubeconfig
  EXECUTOR_KUBECONFIG=.kube/monitor-executor.kubeconfig
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from kubernetes import client, config

from app.agent.tools.base import ToolContext
from app.agent.tools.rollout import GetRolloutStatusInput, get_rollout_status
from app.core.config import Settings
from app.core.k8s import ReadOnlyClients, build_api_client
from app.executor.actions import ActionRunner, ExecutorApi
from app.executor.schemas import ActionRequest
from app.executor.validation import ExecutorPolicy

pytestmark = pytest.mark.integration

NS = "demo"
EXECUTOR_KUBECONFIG = os.environ.get("EXECUTOR_KUBECONFIG")
READER_KUBECONFIG = os.environ.get("MONITOR_READER_KUBECONFIG")


@pytest.fixture(scope="module")
def admin() -> client.AppsV1Api:
    if not (EXECUTOR_KUBECONFIG and READER_KUBECONFIG):
        pytest.skip("EXECUTOR_KUBECONFIG / MONITOR_READER_KUBECONFIG not set")
    loader: Callable[..., client.ApiClient] = config.new_client_from_config
    return client.AppsV1Api(loader())


@pytest.fixture(scope="module")
def executor_api() -> client.ApiClient:
    assert EXECUTOR_KUBECONFIG
    return build_api_client(EXECUTOR_KUBECONFIG, None)


@pytest.fixture(scope="module")
def runner(executor_api: client.ApiClient) -> ActionRunner:
    apps = ExecutorApi(client.AppsV1Api(executor_api))
    return ActionRunner(apps, ExecutorPolicy(allowed_namespaces=frozenset({NS})))


@pytest.fixture(scope="module")
def reader() -> ToolContext:
    assert READER_KUBECONFIG
    api = build_api_client(READER_KUBECONFIG, None)
    return ToolContext(ReadOnlyClients.from_api_client(api, 15), Settings(_env_file=None))


@pytest.fixture
def deployment(admin: client.AppsV1Api) -> Iterator[str]:
    name = f"it-exec-{uuid.uuid4().hex[:6]}"
    labels = {"app": name}
    admin.create_namespaced_deployment(
        NS,
        client.V1Deployment(
            metadata=client.V1ObjectMeta(name=name),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(match_labels=labels),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(labels=labels),
                    spec=client.V1PodSpec(
                        containers=[
                            client.V1Container(
                                name="app",
                                image="busybox:1.36",
                                command=["sh", "-c", "sleep 3600"],
                                resources=client.V1ResourceRequirements(
                                    requests={"cpu": "10m", "memory": "16Mi"},
                                    limits={"memory": "32Mi"},
                                ),
                            )
                        ]
                    ),
                ),
            ),
        ),
    )
    yield name
    admin.delete_namespaced_deployment(name, NS)


def _container(admin: client.AppsV1Api, name: str) -> Any:
    dep: Any = admin.read_namespaced_deployment(name, NS)
    return dep.spec.template.spec.containers[0]


def act(name: str, action_type: str, **params: Any) -> ActionRequest:
    return ActionRequest.model_validate(
        {"action_type": action_type, "namespace": NS, "target": name, "parameters": params}
    )


def test_dry_run_then_apply_patch_resources(
    admin: client.AppsV1Api, runner: ActionRunner, deployment: str
) -> None:
    action = act(deployment, "patch_resources", container="app", memory_limit="64Mi")
    plan = runner.run(action, dry_run=True)
    assert "+          memory: 64Mi" in plan.diff
    assert _container(admin, deployment).resources.limits == {"memory": "32Mi"}  # unchanged

    result = runner.run(action, dry_run=False)
    assert result.status is not None
    assert _container(admin, deployment).resources.limits == {"memory": "64Mi"}


def test_scale(admin: client.AppsV1Api, runner: ActionRunner, deployment: str) -> None:
    runner.run(act(deployment, "scale", replicas=2), dry_run=False)
    dep: Any = admin.read_namespaced_deployment(deployment, NS)
    assert dep.spec.replicas == 2


def test_rollout_undo(
    admin: client.AppsV1Api, runner: ActionRunner, reader: ToolContext, deployment: str
) -> None:
    # Introduce a bad revision (as a broken rollout would), then roll back with the executor.
    admin.patch_namespaced_deployment(
        deployment, NS,
        {"spec": {"template": {"spec": {"containers": [
            {"name": "app", "image": "busybox:does-not-exist"}]}}}},
        _content_type="application/strategic-merge-patch+json",
    )  # fmt: skip
    params = GetRolloutStatusInput(namespace=NS, deployment=deployment)
    deadline = time.monotonic() + 60
    status = get_rollout_status(reader, params)
    while status.previous_revision is None and time.monotonic() < deadline:
        time.sleep(1)
        status = get_rollout_status(reader, params)
    previous = status.previous_revision
    assert previous is not None, status

    runner.run(
        act(deployment, "rollout_undo", replicaset=previous.replicaset,
            to_revision=previous.revision),
        dry_run=False,
    )  # fmt: skip
    assert _container(admin, deployment).image == "busybox:1.36"


@pytest.mark.parametrize(
    ("verb", "resource", "namespace", "allowed"),
    [
        ("patch", "deployments", NS, True),
        ("get", "deployments", NS, True),
        ("get", "replicasets", NS, True),
        ("list", "replicasets", NS, False),
        ("delete", "deployments", NS, False),
        ("create", "deployments", NS, False),
        ("update", "deployments", NS, False),
        ("get", "secrets", NS, False),
        ("get", "pods", NS, False),
        ("patch", "deployments", "default", False),
        ("patch", "deployments", "kube-system", False),
    ],
)
def test_executor_identity_permissions(
    executor_api: client.ApiClient, verb: str, resource: str, namespace: str, allowed: bool
) -> None:
    group = "" if resource in ("secrets", "pods") else "apps"
    review: Any = client.AuthorizationV1Api(executor_api).create_self_subject_access_review(
        client.V1SelfSubjectAccessReview(
            spec=client.V1SelfSubjectAccessReviewSpec(
                resource_attributes=client.V1ResourceAttributes(
                    namespace=namespace, verb=verb, resource=resource, group=group
                )
            )
        )
    )
    assert review.status.allowed is allowed

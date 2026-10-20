"""Builders for realistic kubernetes client model objects used by the mocked-client tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from kubernetes import client as k

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def ts(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def container(
    name: str = "app",
    image: str = "ghcr.io/acme/checkout:1.4.2",
    requests: dict[str, str] | None = None,
    limits: dict[str, str] | None = None,
    env: list[k.V1EnvVar] | None = None,
    liveness: k.V1Probe | None = None,
    command: list[str] | None = None,
) -> k.V1Container:
    return k.V1Container(
        name=name,
        image=image,
        resources=k.V1ResourceRequirements(requests=requests, limits=limits),
        env=env,
        liveness_probe=liveness,
        ports=[k.V1ContainerPort(container_port=8080)],
        command=command,
    )


def oom_status(name: str = "app", restarts: int = 4) -> k.V1ContainerStatus:
    return k.V1ContainerStatus(
        name=name,
        image="ghcr.io/acme/checkout:1.4.2",
        image_id="sha256:abc",
        ready=False,
        restart_count=restarts,
        state=k.V1ContainerState(
            waiting=k.V1ContainerStateWaiting(
                reason="CrashLoopBackOff", message="back-off 1m20s restarting failed container"
            )
        ),
        last_state=k.V1ContainerState(
            terminated=k.V1ContainerStateTerminated(
                reason="OOMKilled", exit_code=137, started_at=ts(1), finished_at=ts(2)
            )
        ),
    )


def running_status(name: str = "app") -> k.V1ContainerStatus:
    return k.V1ContainerStatus(
        name=name,
        image="ghcr.io/acme/checkout:1.4.2",
        image_id="sha256:abc",
        ready=True,
        restart_count=0,
        state=k.V1ContainerState(running=k.V1ContainerStateRunning(started_at=ts(0))),
        last_state=k.V1ContainerState(),
    )


def pod(
    name: str,
    namespace: str = "demo",
    containers: list[k.V1Container] | None = None,
    statuses: list[k.V1ContainerStatus] | None = None,
    phase: str = "Running",
    conditions: list[k.V1PodCondition] | None = None,
    node: str | None = "kind-worker",
) -> k.V1Pod:
    return k.V1Pod(
        metadata=k.V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels={"app": "checkout"},
            creation_timestamp=T0,
            owner_references=[
                k.V1OwnerReference(
                    api_version="apps/v1", kind="ReplicaSet", name="checkout-7d9f", uid="rs-uid"
                )
            ],
        ),
        spec=k.V1PodSpec(containers=containers or [container()], node_name=node),
        status=k.V1PodStatus(
            phase=phase,
            qos_class="Burstable",
            container_statuses=statuses,
            conditions=conditions,
        ),
    )


def event(
    reason: str,
    message: str,
    obj: str = "checkout-7d9f-abc",
    kind: str = "Pod",
    etype: str = "Warning",
    first: int = 0,
    last: int = 0,
    count: int = 1,
    uid: str = "e",
) -> k.CoreV1Event:
    return k.CoreV1Event(
        metadata=k.V1ObjectMeta(
            name=f"{obj}.{uid}", namespace="demo", creation_timestamp=ts(first)
        ),
        involved_object=k.V1ObjectReference(kind=kind, name=obj, namespace="demo"),
        reason=reason,
        message=message,
        type=etype,
        count=count,
        first_timestamp=ts(first),
        last_timestamp=ts(last),
    )


def deployment(
    name: str = "checkout",
    revision: int = 3,
    replicas: int = 3,
    updated: int = 1,
    ready: int = 2,
    available: int = 2,
    conditions: list[k.V1DeploymentCondition] | None = None,
) -> k.V1Deployment:
    return k.V1Deployment(
        metadata=k.V1ObjectMeta(
            name=name,
            namespace="demo",
            uid="dep-uid",
            generation=5,
            annotations={"deployment.kubernetes.io/revision": str(revision)},
        ),
        spec=k.V1DeploymentSpec(
            replicas=replicas,
            selector=k.V1LabelSelector(match_labels={"app": name}),
            template=k.V1PodTemplateSpec(spec=k.V1PodSpec(containers=[container()])),
        ),
        status=k.V1DeploymentStatus(
            observed_generation=5,
            replicas=replicas,
            updated_replicas=updated,
            ready_replicas=ready,
            available_replicas=available,
            unavailable_replicas=replicas - available,
            conditions=conditions,
        ),
    )


def replicaset(
    name: str,
    revision: int,
    image: str,
    owner_uid: str = "dep-uid",
    replicas: int = 1,
    ready: int = 1,
    env: list[k.V1EnvVar] | None = None,
) -> k.V1ReplicaSet:
    return k.V1ReplicaSet(
        metadata=k.V1ObjectMeta(
            name=name,
            namespace="demo",
            annotations={"deployment.kubernetes.io/revision": str(revision)},
            creation_timestamp=ts(revision),
            owner_references=[
                k.V1OwnerReference(
                    api_version="apps/v1", kind="Deployment", name="checkout", uid=owner_uid
                )
            ],
        ),
        spec=k.V1ReplicaSetSpec(
            selector=k.V1LabelSelector(match_labels={"app": "checkout"}),
            template=k.V1PodTemplateSpec(
                spec=k.V1PodSpec(containers=[container(image=image, env=env)])
            ),
        ),
        status=k.V1ReplicaSetStatus(replicas=replicas, ready_replicas=ready),
    )


def pod_list(*pods: k.V1Pod) -> k.V1PodList:
    return k.V1PodList(items=list(pods))


def api_error(status: int, message: str, reason: str = "Error") -> Any:
    import json

    from kubernetes.client.exceptions import ApiException

    exc = ApiException(status=status, reason=reason)
    exc.body = json.dumps({"kind": "Status", "message": message})
    return exc

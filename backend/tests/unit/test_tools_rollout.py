from kubernetes import client as k

from app.agent.tools.rollout import GetRolloutStatusInput, get_rollout_status
from tests.conftest import FakeCluster
from tests.unit import factories as f


def _setup(cluster: FakeCluster) -> None:
    cluster.apps.read_namespaced_deployment.return_value = f.deployment(
        revision=3,
        conditions=[
            k.V1DeploymentCondition(
                type="Progressing",
                status="False",
                reason="ProgressDeadlineExceeded",
                message='ReplicaSet "checkout-v3" has timed out progressing.',
            )
        ],
    )
    cluster.apps.list_namespaced_replica_set.return_value = k.V1ReplicaSetList(
        items=[
            f.replicaset("checkout-v1", 1, "ghcr.io/acme/checkout:1.3.0", replicas=0, ready=0),
            f.replicaset("checkout-v3", 3, "ghcr.io/acme/checkout:1.5.0-typo", replicas=1, ready=0),
            f.replicaset(
                "checkout-v2",
                2,
                "ghcr.io/acme/checkout:1.4.2",
                replicas=2,
                ready=2,
                env=[k.V1EnvVar(name="DATABASE_URL", value="x")],
            ),
            f.replicaset("someone-else", 9, "other:1", owner_uid="other-uid"),
        ]
    )


def test_rollout_status_identifies_current_and_previous_revisions(cluster: FakeCluster) -> None:
    _setup(cluster)
    out = get_rollout_status(
        cluster.ctx, GetRolloutStatusInput(namespace="demo", deployment="checkout")
    )
    cluster.apps.list_namespaced_replica_set.assert_called_once_with(
        "demo", label_selector="app=checkout", _request_timeout=5.0
    )
    assert out.desired_replicas == 3
    assert out.updated_replicas == 1
    assert out.available_replicas == 2
    assert out.rollout_complete is False
    assert out.conditions[0].reason == "ProgressDeadlineExceeded"
    assert out.current_revision is not None and out.current_revision.replicaset == "checkout-v3"
    assert out.previous_revision is not None and out.previous_revision.replicaset == "checkout-v2"
    # ReplicaSets not owned by this deployment are ignored.
    assert [r.revision for r in out.revision_history] == [3, 2, 1]


def test_rollout_status_diffs_previous_and_current(cluster: FakeCluster) -> None:
    _setup(cluster)
    out = get_rollout_status(
        cluster.ctx, GetRolloutStatusInput(namespace="demo", deployment="checkout")
    )
    changes = {c.field: c for c in out.changes_from_previous}
    assert changes["image"].previous == "ghcr.io/acme/checkout:1.4.2"
    assert changes["image"].current == "ghcr.io/acme/checkout:1.5.0-typo"
    assert changes["env"].previous == ["DATABASE_URL"]
    assert changes["env"].current == []


def test_rollout_complete_with_single_revision(cluster: FakeCluster) -> None:
    cluster.apps.read_namespaced_deployment.return_value = f.deployment(
        revision=1, replicas=2, updated=2, ready=2, available=2
    )
    cluster.apps.list_namespaced_replica_set.return_value = k.V1ReplicaSetList(
        items=[f.replicaset("checkout-v1", 1, "img:1", replicas=2, ready=2)]
    )
    out = get_rollout_status(
        cluster.ctx, GetRolloutStatusInput(namespace="demo", deployment="checkout")
    )
    assert out.rollout_complete is True
    assert out.previous_revision is None
    assert out.changes_from_previous == []

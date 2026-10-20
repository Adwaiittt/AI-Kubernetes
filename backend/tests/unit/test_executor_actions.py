import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.executor.actions import ActionRunner, ExecutionError, ExecutorApi
from app.executor.schemas import ActionRequest
from app.executor.validation import ActionRejectedError, ExecutorPolicy
from tests.unit import factories as f
from tests.unit.executor_fakes import FakeApps

POLICY = ExecutorPolicy(allowed_namespaces=frozenset({"demo"}))
FIXED = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


@pytest.fixture
def apps() -> FakeApps:
    return FakeApps()


@pytest.fixture
def runner(apps: FakeApps) -> ActionRunner:
    return ActionRunner(ExecutorApi(apps), POLICY, clock=lambda: FIXED)


def act(action_type: str, **params: Any) -> ActionRequest:
    return ActionRequest.model_validate(
        {
            "action_type": action_type,
            "namespace": "demo",
            "target": "checkout",
            "parameters": params,
        }
    )


def test_patch_resources_dry_run(runner: ActionRunner, apps: FakeApps) -> None:
    result = runner.run(act("patch_resources", container="app", memory_limit="512Mi"), dry_run=True)

    (call,) = apps.patches
    assert call["dry_run"] == "All"
    assert call["content_type"] == "application/strategic-merge-patch+json"
    assert call["field_manager"] == "k8s-ai-monitor-executor"
    assert call["body"] == {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{"name": "app", "resources": {"limits": {"memory": "512Mi"}}}]
                }
            }
        }
    }
    assert result.dry_run is True
    assert result.status is None
    assert "-          memory: 128Mi" in result.diff
    assert "+          memory: 512Mi" in result.diff
    # A dry run changes nothing.
    stored = apps.view("demo", "checkout")
    assert stored["template"]["spec"]["containers"][0]["resources"]["limits"] == {"memory": "128Mi"}


def test_patch_resources_apply_records_status(runner: ActionRunner, apps: FakeApps) -> None:
    result = runner.run(
        act("patch_resources", container="app", memory_limit="512Mi", memory_request="256Mi"),
        dry_run=False,
    )
    assert apps.applied[0]["dry_run"] is None
    assert result.status is not None and result.status.generation == 6
    assert result.after["template"]["spec"]["containers"][0]["resources"] == {
        "limits": {"memory": "512Mi"},
        "requests": {"cpu": "100m", "memory": "256Mi"},
    }


def test_patch_resources_unknown_container(runner: ActionRunner, apps: FakeApps) -> None:
    with pytest.raises(ActionRejectedError, match="container 'sidecar' not found"):
        runner.run(act("patch_resources", container="sidecar", memory_limit="512Mi"), dry_run=True)
    assert apps.patches == []


def test_new_limit_below_existing_request_is_refused(runner: ActionRunner) -> None:
    with pytest.raises(ActionRejectedError, match="request 64Mi would exceed the limit 32Mi"):
        runner.plan(act("patch_resources", container="app", memory_limit="32Mi"))


def test_scale(runner: ActionRunner, apps: FakeApps) -> None:
    result = runner.run(act("scale", replicas=5), dry_run=False)
    assert apps.applied[0]["body"] == {"spec": {"replicas": 5}}
    assert "-replicas: 2" in result.diff and "+replicas: 5" in result.diff


def test_rollout_restart(runner: ActionRunner, apps: FakeApps) -> None:
    runner.run(act("rollout_restart"), dry_run=True)
    assert apps.patches[0]["body"] == {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {"kubectl.kubernetes.io/restartedAt": FIXED.isoformat()}
                }
            }
        }
    }


def test_rollout_undo(runner: ActionRunner, apps: FakeApps) -> None:
    result = runner.run(act("rollout_undo", replicaset="checkout-v2", to_revision=2), dry_run=False)
    (call,) = apps.applied
    assert call["content_type"] == "application/json-patch+json"
    (op,) = call["body"]
    assert op["op"] == "replace" and op["path"] == "/spec/template"
    assert op["value"]["spec"]["containers"][0]["image"] == "ghcr.io/acme/checkout:1.4.2"
    assert "pod-template-hash" not in op["value"]["metadata"]["labels"]
    assert "+    - image: ghcr.io/acme/checkout:1.4.2" in result.diff


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({}, "requires a resolved replicaset"),
        ({"replicaset": "missing"}, "not found"),
        ({"replicaset": "stranger"}, "not owned by deployment"),
        ({"replicaset": "checkout-v3"}, "not an earlier revision"),
        ({"replicaset": "checkout-v2", "to_revision": 1}, "is revision 2, not 1"),
    ],
)
def test_rollout_undo_is_verified(
    runner: ActionRunner, apps: FakeApps, params: dict[str, Any], message: str
) -> None:
    with pytest.raises(ActionRejectedError, match=message):
        runner.run(act("rollout_undo", **params), dry_run=True)
    assert apps.patches == []


def test_missing_deployment(runner: ActionRunner) -> None:
    req = ActionRequest(
        action_type="scale", namespace="demo", target="ghost", parameters={"replicas": 2}
    )
    with pytest.raises(ActionRejectedError, match="demo/ghost not found"):
        runner.run(req, dry_run=True)


def test_kubernetes_rejection_is_an_execution_error(runner: ActionRunner, apps: FakeApps) -> None:
    apps.fail_patch = f.api_error(403, "forbidden: cannot patch")
    with pytest.raises(ExecutionError, match="403: forbidden"):
        runner.run(act("scale", replicas=3), dry_run=False)


def test_read_errors_other_than_404(runner: ActionRunner, apps: FakeApps) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise f.api_error(500, "etcd unavailable")

    apps.read_namespaced_deployment = boom  # type: ignore[method-assign]
    with pytest.raises(ExecutionError, match="500: etcd unavailable"):
        runner.plan(act("scale", replicas=3))


@pytest.mark.parametrize(
    "method",
    [
        "delete_namespaced_deployment",
        "list_namespaced_replica_set",
        "create_namespaced_deployment",
        "replace_namespaced_deployment",
        "api_client",
    ],
)
def test_executor_api_allows_only_three_methods(method: str) -> None:
    api = ExecutorApi(FakeApps())
    with pytest.raises(PermissionError):
        getattr(api, method)
    with pytest.raises(PermissionError):
        api.x = 1


def test_executor_package_never_imports_the_agent() -> None:
    pkg = Path(__file__).resolve().parents[2] / "app" / "executor"
    offenders = []
    for path in pkg.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            offenders += [f"{path.name}: {n}" for n in names if n.startswith("app.agent")]
    assert offenders == []

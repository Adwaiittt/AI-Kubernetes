from typing import Any

import pytest

from app.executor.schemas import ActionRequest
from app.executor.validation import (
    ActionRejectedError,
    ExecutorPolicy,
    PatchResourcesParams,
    ScaleParams,
    validate_action,
)

POLICY = ExecutorPolicy(
    allowed_namespaces=frozenset({"demo"}),
    max_memory="2Gi",
    max_cpu="2",
    min_replicas=1,
    max_replicas=10,
)


def req(
    action_type: str = "patch_resources",
    namespace: str = "demo",
    target: str = "checkout",
    **parameters: Any,
) -> ActionRequest:
    return ActionRequest.model_validate(
        {
            "action_type": action_type,
            "namespace": namespace,
            "target": target,
            "parameters": parameters,
        }
    )


def test_valid_patch_resources() -> None:
    v = validate_action(req(container="app", memory_limit="512Mi", memory_request="256Mi"), POLICY)
    assert isinstance(v.params, PatchResourcesParams)
    assert v.params.memory_limit == "512Mi"


def test_valid_scale() -> None:
    v = validate_action(req("scale", replicas=3), POLICY)
    assert isinstance(v.params, ScaleParams) and v.params.replicas == 3


@pytest.mark.parametrize("namespace", ["kube-system", "default", "payments"])
def test_non_allowlisted_namespace_is_refused(namespace: str) -> None:
    with pytest.raises(ActionRejectedError, match="not in the executor allowlist"):
        validate_action(req(namespace=namespace, container="app", memory_limit="512Mi"), POLICY)


@pytest.mark.parametrize(
    ("action_type", "params", "message"),
    [
        ("patch_resources", {"memory_limit": "512Mi"}, "container"),
        ("patch_resources", {"container": "app"}, "at least one"),
        ("patch_resources", {"container": "app", "memory_limit": "lots"}, "not a valid"),
        ("patch_resources", {"container": "app", "memory_limit": "1e9"}, "not a valid"),
        ("patch_resources", {"container": "app", "memory_limit": "-512Mi"}, "not a valid"),
        ("patch_resources", {"container": "app", "memory_limit": "0"}, "must be positive"),
        ("patch_resources", {"container": "app", "memory_limit": "8Gi"}, "exceeds"),
        ("patch_resources", {"container": "app", "cpu_limit": "16"}, "exceeds"),
        (
            "patch_resources",
            {"container": "app", "memory_limit": "256Mi", "memory_request": "512Mi"},
            "must not exceed",
        ),
        (
            "patch_resources",
            {"container": "app", "memory_limit": "512Mi", "image": "evil:1"},
            "Extra inputs",
        ),
        ("patch_resources", {"container": "App_1", "memory_limit": "512Mi"}, "container"),
        ("patch_resources", {"container": "app", "memory_limit": 512}, "memory_limit"),
        ("scale", {"replicas": 0}, "between 1 and 10"),
        ("scale", {"replicas": 11}, "between 1 and 10"),
        ("scale", {"replicas": "3"}, "replicas"),
        ("scale", {"replicas": True}, "replicas"),
        ("scale", {}, "replicas"),
        ("rollout_restart", {"force": True}, "Extra inputs"),
        ("rollout_undo", {"to_revision": 0}, "to_revision"),
        ("rollout_undo", {"replicaset": "../../etc"}, "replicaset"),
    ],
)
def test_invalid_parameters_are_refused(
    action_type: str, params: dict[str, Any], message: str
) -> None:
    with pytest.raises(ActionRejectedError, match=message):
        validate_action(req(action_type, **params), POLICY)


def test_invalid_target_is_refused() -> None:
    with pytest.raises(ActionRejectedError, match="not a valid deployment name"):
        validate_action(req(target="Checkout; rm -rf", container="app", memory_limit="1Gi"), POLICY)


def test_unknown_action_type_is_refused_by_schema() -> None:
    with pytest.raises(ValueError):
        ActionRequest.model_validate({"action_type": "delete", "namespace": "demo", "target": "x"})


def test_action_digest_is_canonical() -> None:
    a = req(container="app", memory_limit="512Mi", cpu_limit="1")
    b = ActionRequest.model_validate(
        {
            "target": "checkout",
            "parameters": {"cpu_limit": "1", "memory_limit": "512Mi", "container": "app"},
            "namespace": "demo",
            "action_type": "patch_resources",
        }
    )
    assert a.digest() == b.digest()
    assert a.digest() != req(container="app", memory_limit="513Mi", cpu_limit="1").digest()

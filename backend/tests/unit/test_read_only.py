"""The investigation side must be unable to mutate cluster state, at three layers:
RBAC (test_rbac_manifests.py), the ReadOnlyApi proxy, and the tool source code itself."""

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from kubernetes import client

from app.core.k8s import ReadOnlyApi, ReadOnlyClients, ReadOnlyViolationError

TOOLS_DIR = Path(__file__).resolve().parents[2] / "app" / "agent" / "tools"
MUTATING_PREFIXES = ("create_", "patch_", "replace_", "delete_", "connect_")


@pytest.mark.parametrize(
    "method",
    [
        "delete_namespaced_pod",
        "patch_namespaced_deployment",
        "create_namespaced_pod_eviction",
        "replace_namespaced_deployment_scale",
        "delete_collection_namespaced_pod",
        "connect_get_namespaced_pod_exec",
        "api_client",
    ],
)
def test_proxy_blocks_non_read_methods(method: str) -> None:
    api = ReadOnlyApi(MagicMock())
    with pytest.raises(ReadOnlyViolationError):
        getattr(api, method)


def test_proxy_forwards_read_methods() -> None:
    inner = MagicMock()
    api = ReadOnlyApi(inner)
    api.list_namespaced_pod("demo")
    api.read_namespaced_pod_log("p", "demo")
    api.get_namespaced_custom_object("g", "v", "demo", "pods", "p")
    inner.list_namespaced_pod.assert_called_once_with("demo")


def test_proxy_is_immutable() -> None:
    api = ReadOnlyApi(MagicMock())
    with pytest.raises(ReadOnlyViolationError):
        api.patch_namespaced_pod = lambda: None


def test_clients_wrap_real_api_classes() -> None:
    clients = ReadOnlyClients.from_api_client(client.ApiClient(), request_timeout=3)
    assert isinstance(clients.core, ReadOnlyApi)
    with pytest.raises(ReadOnlyViolationError):
        clients.apps.patch_namespaced_deployment  # noqa: B018
    assert callable(clients.core.list_namespaced_pod)


def test_tool_sources_reference_no_mutating_api_methods() -> None:
    offenders: list[str] = []
    for path in TOOLS_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr.startswith(MUTATING_PREFIXES):
                offenders.append(f"{path.name}:{node.lineno} {node.attr}")
    assert offenders == []

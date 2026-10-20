"""Static checks on the RBAC manifests. These encode the security model; never relax them to make
something else pass."""

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
READER_MANIFEST = REPO / "deploy" / "k8s" / "10-reader-rbac.yaml"
READ_VERBS = {"get", "list", "watch"}
FORBIDDEN_RESOURCES = {
    "secrets",
    "configmaps",
    "pods/exec",
    "pods/attach",
    "pods/portforward",
    "serviceaccounts/token",
    "*",
}


def _docs(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


@pytest.fixture
def reader_docs() -> list[dict[str, Any]]:
    return _docs(READER_MANIFEST)


def _roles(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in docs if d["kind"] in {"Role", "ClusterRole"}]


def test_reader_role_has_only_read_verbs(reader_docs: list[dict[str, Any]]) -> None:
    roles = _roles(reader_docs)
    assert roles, "reader Role not found"
    for role in roles:
        for rule in role["rules"]:
            verbs = set(rule["verbs"])
            assert verbs <= READ_VERBS, f"non-read verbs in {role['metadata']['name']}: {verbs}"
            assert not verbs & {"create", "update", "patch", "delete", "deletecollection", "*"}


def test_reader_role_never_touches_sensitive_resources(reader_docs: list[dict[str, Any]]) -> None:
    for role in _roles(reader_docs):
        for rule in role["rules"]:
            assert not set(rule["resources"]) & FORBIDDEN_RESOURCES
            assert "*" not in rule.get("apiGroups", [])
            assert "resourceNames" not in rule or rule["resourceNames"]


def test_reader_grants_exactly_what_the_tools_need(reader_docs: list[dict[str, Any]]) -> None:
    granted = {
        (group, resource)
        for role in _roles(reader_docs)
        for rule in role["rules"]
        for group in rule["apiGroups"]
        for resource in rule["resources"]
    }
    assert granted == {
        ("", "pods"),
        ("", "pods/log"),
        ("", "events"),
        ("apps", "deployments"),
        ("apps", "replicasets"),
        ("metrics.k8s.io", "pods"),
    }


def test_reader_is_namespace_scoped(reader_docs: list[dict[str, Any]]) -> None:
    kinds = {d["kind"] for d in reader_docs}
    assert "ClusterRole" not in kinds
    assert "ClusterRoleBinding" not in kinds
    for binding in (d for d in reader_docs if d["kind"] == "RoleBinding"):
        assert binding["roleRef"]["kind"] == "Role"
        assert binding["subjects"] == [
            {"kind": "ServiceAccount", "name": "monitor-reader", "namespace": "k8s-ai-monitor"}
        ]


def test_no_manifest_binds_reader_to_anything_cluster_wide() -> None:
    for path in (REPO / "deploy" / "k8s").glob("*.yaml"):
        for doc in _docs(path):
            if doc["kind"] == "ClusterRoleBinding":
                names = {s.get("name") for s in doc.get("subjects", [])}
                assert "monitor-reader" not in names, path.name

"""Static policy checks on the in-cluster workload manifests (deploy/k8s/30-50)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

K8S = Path(__file__).resolve().parents[3] / "deploy" / "k8s"


def _docs(name: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all((K8S / name).read_text(encoding="utf-8")) if d]


def _deployment(name: str) -> dict[str, Any]:
    return next(d for d in _docs(name) if d["kind"] == "Deployment")


@pytest.mark.parametrize(
    ("manifest", "service_account"),
    [("30-api.yaml", "monitor-reader"), ("40-executor.yaml", "monitor-executor")],
)
def test_each_component_runs_as_its_own_identity(manifest: str, service_account: str) -> None:
    spec = _deployment(manifest)["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == service_account


def test_dashboard_has_no_kubernetes_credentials() -> None:
    spec = _deployment("50-dashboard.yaml")["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert "serviceAccountName" not in spec


@pytest.mark.parametrize("manifest", ["30-api.yaml", "40-executor.yaml", "50-dashboard.yaml"])
def test_restricted_pod_security(manifest: str) -> None:
    spec = _deployment(manifest)["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    for c in spec["containers"]:
        sc = c["securityContext"]
        assert sc["allowPrivilegeEscalation"] is False
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["capabilities"] == {"drop": ["ALL"]}
        assert "limits" in c["resources"] and "memory" in c["resources"]["limits"]
        assert not c.get("securityContext", {}).get("privileged", False)


def test_executor_is_reachable_only_from_the_api() -> None:
    policy = next(d for d in _docs("40-executor.yaml") if d["kind"] == "NetworkPolicy")
    assert policy["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "executor"}
    (rule,) = policy["spec"]["ingress"]
    assert rule["from"] == [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "api"}}}]


def test_executor_gets_only_its_two_secrets() -> None:
    container = _deployment("40-executor.yaml")["spec"]["template"]["spec"]["containers"][0]
    assert "envFrom" not in container  # never the whole Secret (it holds the Claude API key)
    keys = {e["valueFrom"]["secretKeyRef"]["key"] for e in container["env"] if "valueFrom" in e}
    assert keys == {"EXECUTOR_API_KEY", "APPROVAL_SIGNING_KEY"}


def test_no_secret_values_in_manifests() -> None:
    for path in K8S.glob("*.yaml"):
        assert all(d["kind"] != "Secret" for d in _docs(path.name)), path.name

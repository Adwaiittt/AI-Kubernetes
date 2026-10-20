"""Cluster side of the benchmark: per-run namespaces, scenario deployment, waits, cleanup.

The harness itself uses your *admin* kubeconfig (it creates namespaces). The agent never does:
it gets a short-lived token for the monitor-reader ServiceAccount, scoped to the run's
namespace by a copy of the reader Role/RoleBinding.
"""

from __future__ import annotations

import copy
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from chaos.scenario import Scenario, failure_reached

REPO = Path(__file__).resolve().parents[1]
READER_RBAC = REPO / "deploy" / "k8s" / "10-reader-rbac.yaml"
MONITOR_NS = "k8s-ai-monitor"
READER_SA = "monitor-reader"
LABEL = "k8s-ai-monitor/chaos"
SMP = "application/strategic-merge-patch+json"


class SetupError(RuntimeError):
    pass


def admin_api_client(kubeconfig: str | None = None, context: str | None = None) -> client.ApiClient:
    loader: Callable[..., client.ApiClient] = config.new_client_from_config
    return loader(config_file=kubeconfig, context=context)


def run_namespace(scenario: Scenario) -> str:
    slug = scenario.id.split("-", 1)[-1][:30]
    return f"chaos-{slug}-{uuid.uuid4().hex[:6]}"


def reader_rbac_docs(namespace: str) -> list[dict[str, Any]]:
    """The monitor-reader Role + RoleBinding from deploy/k8s, re-targeted at ``namespace``."""
    docs = [d for d in yaml.safe_load_all(READER_RBAC.read_text(encoding="utf-8")) if d]
    out = []
    for doc in docs:
        if doc["kind"] in ("Role", "RoleBinding"):
            doc = copy.deepcopy(doc)
            doc["metadata"]["namespace"] = namespace
            out.append(doc)
    if {d["kind"] for d in out} != {"Role", "RoleBinding"}:
        raise SetupError(f"{READER_RBAC} must define the reader Role and RoleBinding")
    return out


class Cluster:
    def __init__(self, api_client: client.ApiClient) -> None:
        self.api_client = api_client
        self.core = client.CoreV1Api(api_client)
        self.apps = client.AppsV1Api(api_client)
        self.rbac = client.RbacAuthorizationV1Api(api_client)

    # --- namespace lifecycle ------------------------------------------------------------

    def create_namespace(self, name: str, scenario_id: str) -> None:
        self.core.create_namespace(
            client.V1Namespace(metadata=client.V1ObjectMeta(name=name, labels={LABEL: scenario_id}))
        )
        for doc in reader_rbac_docs(name):
            if doc["kind"] == "Role":
                self.rbac.create_namespaced_role(name, cast(Any, doc))
            else:
                self.rbac.create_namespaced_role_binding(name, cast(Any, doc))

    def delete_namespace(self, name: str) -> None:
        try:
            self.core.delete_namespace(name, propagation_policy="Background")
        except ApiException as exc:
            if exc.status != 404:
                raise

    def leftover_namespaces(self) -> list[str]:
        items: list[Any] = self.core.list_namespace(label_selector=LABEL).items
        return [str(ns.metadata.name) for ns in items]

    # --- scenario deployment ------------------------------------------------------------

    def deploy(self, scenario: Scenario, namespace: str, timeout: float = 120) -> None:
        for step, docs in scenario.manifests():
            for doc in docs:
                self._apply(doc, namespace)
            if step.wait == "available":
                self.wait_available(namespace, scenario.target, timeout)

    def _apply(self, doc: dict[str, Any], namespace: str) -> None:
        if doc.get("kind") != "Deployment":
            raise SetupError(
                f"scenario manifests may only contain Deployments, got {doc.get('kind')}"
            )
        doc = copy.deepcopy(doc)
        doc.setdefault("metadata", {})["namespace"] = namespace
        name = doc["metadata"]["name"]
        try:
            self.apps.read_namespaced_deployment(name, namespace)
        except ApiException as exc:
            if exc.status != 404:
                raise
            self.apps.create_namespaced_deployment(namespace, cast(Any, doc))
            return
        self.apps.patch_namespaced_deployment(name, namespace, doc, _content_type=SMP)

    def wait_available(self, namespace: str, name: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            dep: Any = self.apps.read_namespaced_deployment(name, namespace)
            want = dep.spec.replicas or 0
            s = dep.status
            if s and (s.available_replicas or 0) >= want and (s.updated_replicas or 0) >= want:
                return
            time.sleep(2)
        raise SetupError(f"deployment {namespace}/{name} not available after {timeout:.0f}s")

    def wait_for_failure(self, scenario: Scenario, namespace: str) -> str:
        cond = scenario.failure
        deadline = time.monotonic() + cond.timeout_seconds
        while time.monotonic() < deadline:
            pods = self.core.list_namespaced_pod(namespace, label_selector=f"app={scenario.target}")
            observed = failure_reached(cond, pods.items)
            if observed:
                return observed
            time.sleep(3)
        raise SetupError(
            f"failure state {cond.type} {cond.reasons or ''} not reached in {cond.timeout_seconds}s"
        )

    # --- the agent's identity -----------------------------------------------------------

    def reader_api_client(self, ttl_seconds: int = 3600) -> client.ApiClient:
        """An ApiClient authenticated as monitor-reader via a short-lived TokenRequest."""
        request = client.AuthenticationV1TokenRequest(
            spec=client.V1TokenRequestSpec(audiences=[], expiration_seconds=ttl_seconds)
        )
        token: Any = self.core.create_namespaced_service_account_token(
            READER_SA, MONITOR_NS, request
        )
        admin_cfg = self.api_client.configuration
        cfg = client.Configuration()
        cfg.host = admin_cfg.host
        cfg.ssl_ca_cert = admin_cfg.ssl_ca_cert
        cfg.verify_ssl = admin_cfg.verify_ssl
        cfg.api_key = {"BearerToken": f"Bearer {token.status.token}"}
        return client.ApiClient(cfg)

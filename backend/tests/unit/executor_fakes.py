"""In-memory stand-in for the executor's AppsV1Api that really applies our patch shapes."""

from __future__ import annotations

import copy
from typing import Any

from kubernetes import client as k

from tests.unit import factories as f

SIGNING_KEY = b"k" * 32
EXECUTOR_KEY = "executor-test-key-0123"

_api: Any = k.ApiClient()


class FakeApps:
    def __init__(self) -> None:
        dep: Any = f.deployment(revision=3, replicas=2)
        dep.spec.template.metadata = k.V1ObjectMeta(labels={"app": "checkout"})
        # Current revision (3) runs the bad image; revision 2 (checkout-v2) is the good one.
        dep.spec.template.spec.containers = [
            f.container(
                image="ghcr.io/acme/checkout:bad",
                requests={"memory": "64Mi", "cpu": "100m"},
                limits={"memory": "128Mi"},
            )
        ]
        self.deployments: dict[tuple[str, str], Any] = {("demo", "checkout"): dep}
        good: Any = f.replicaset("checkout-v2", 2, "ghcr.io/acme/checkout:1.4.2")
        good.spec.template.metadata = k.V1ObjectMeta(
            labels={"app": "checkout", "pod-template-hash": "abc123"}
        )
        self.replicasets: dict[tuple[str, str], Any] = {
            ("demo", "checkout-v2"): good,
            ("demo", "checkout-v3"): f.replicaset("checkout-v3", 3, "ghcr.io/acme/checkout:bad"),
            ("demo", "stranger"): f.replicaset("stranger", 1, "x:1", owner_uid="other-uid"),
        }
        self.patches: list[dict[str, Any]] = []  # every patch call, dry-run or not
        self.fail_patch: Exception | None = None

    @property
    def applied(self) -> list[dict[str, Any]]:
        return [p for p in self.patches if p["dry_run"] is None]

    def view(self, namespace: str, name: str) -> dict[str, Any]:
        spec: dict[str, Any] = _api.sanitize_for_serialization(self.deployments[(namespace, name)])[
            "spec"
        ]
        return spec

    def read_namespaced_deployment(self, name: str, namespace: str, **_: Any) -> Any:
        try:
            return copy.deepcopy(self.deployments[(namespace, name)])
        except KeyError:
            raise f.api_error(404, f'deployments.apps "{name}" not found') from None

    def read_namespaced_replica_set(self, name: str, namespace: str, **_: Any) -> Any:
        try:
            return copy.deepcopy(self.replicasets[(namespace, name)])
        except KeyError:
            raise f.api_error(404, f'replicasets.apps "{name}" not found') from None

    def patch_namespaced_deployment(
        self, name: str, namespace: str, body: Any, **kwargs: Any
    ) -> Any:
        self.patches.append(
            {
                "name": name,
                "namespace": namespace,
                "body": copy.deepcopy(body),
                "dry_run": kwargs.get("dry_run"),
                "content_type": kwargs.get("_content_type"),
                "field_manager": kwargs.get("field_manager"),
            }
        )
        if self.fail_patch is not None:
            raise self.fail_patch
        dep = self.read_namespaced_deployment(name, namespace)
        if isinstance(body, list):  # json-patch: replace /spec/template
            (op,) = body
            assert op["op"] == "replace" and op["path"] == "/spec/template"
            dep.spec.template = _api._ApiClient__deserialize(op["value"], "V1PodTemplateSpec")
        else:
            self._apply_strategic_merge(dep, body)
        if kwargs.get("dry_run") is None:
            dep.metadata.generation = (dep.metadata.generation or 0) + 1
            self.deployments[(namespace, name)] = copy.deepcopy(dep)
        return dep

    @staticmethod
    def _apply_strategic_merge(dep: Any, body: dict[str, Any]) -> None:
        """Just enough strategic-merge semantics for the executor's patch shapes."""
        spec = body.get("spec", {})
        if "replicas" in spec:
            dep.spec.replicas = spec["replicas"]
        tmpl = spec.get("template", {})
        annotations = tmpl.get("metadata", {}).get("annotations")
        if annotations:
            meta = dep.spec.template.metadata or k.V1ObjectMeta()
            meta.annotations = {**(meta.annotations or {}), **annotations}
            dep.spec.template.metadata = meta
        for c_patch in tmpl.get("spec", {}).get("containers", []):
            target = next(c for c in dep.spec.template.spec.containers if c.name == c_patch["name"])
            res = c_patch.get("resources", {})
            target.resources.limits = {**(target.resources.limits or {}), **res.get("limits", {})}
            target.resources.requests = {
                **(target.resources.requests or {}),
                **res.get("requests", {}),
            }

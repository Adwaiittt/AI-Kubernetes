"""Turn a validated action into an exact Deployment patch, dry-run it or apply it, and diff.

The executor's Kubernetes access is an explicit three-method allowlist: get deployment, get
replicaset, patch deployment. That matches the ``monitor-executor`` RBAC Role (get/patch on
deployments, get on replicasets). In particular the executor cannot *list* replicasets, so for
``rollout_undo`` the backend resolves the target ReplicaSet name with its reader identity and
the executor verifies it by GET: it must be owned by this deployment and be an earlier revision.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import yaml
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from app.executor.schemas import ActionRequest, DeploymentStatus, PatchType, PlanResult
from app.executor.validation import (
    ActionRejectedError,
    ExecutorPolicy,
    PatchResourcesParams,
    RolloutRestartParams,
    RolloutUndoParams,
    ScaleParams,
    ValidatedAction,
    parse_quantity,
    validate_action,
)

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
RESTARTED_AT_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
FIELD_MANAGER = "k8s-ai-monitor-executor"
_CONTENT_TYPES: dict[PatchType, str] = {
    "strategic-merge": "application/strategic-merge-patch+json",
    "json-patch": "application/json-patch+json",
}


class ExecutionError(RuntimeError):
    """Kubernetes refused or failed the patch (message is safe to show)."""


class ExecutorApi:
    """Proxy exposing only the three AppsV1Api methods the executor needs."""

    ALLOWED = frozenset(
        {"read_namespaced_deployment", "read_namespaced_replica_set", "patch_namespaced_deployment"}
    )
    __slots__ = ("_api",)

    def __init__(self, api: Any) -> None:
        object.__setattr__(self, "_api", api)

    def __getattr__(self, name: str) -> Any:
        if name not in self.ALLOWED:
            raise PermissionError(f"executor may not call {name}")
        return getattr(self._api, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise PermissionError("ExecutorApi is immutable")


@dataclass(frozen=True)
class Plan:
    action: ValidatedAction
    patch_type: PatchType
    patch: Any
    before: Any  # V1Deployment


class ActionRunner:
    def __init__(
        self,
        apps: ExecutorApi,
        policy: ExecutorPolicy,
        *,
        request_timeout: float = 10.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._apps = apps
        self._policy = policy
        self._timeout = request_timeout
        self._clock = clock
        # The kubernetes package is untyped; bind its serializer to a typed name.
        self._sanitize: Callable[[Any], Any] = client.ApiClient().sanitize_for_serialization

    # --- public -------------------------------------------------------------------------

    @property
    def policy(self) -> ExecutorPolicy:
        return self._policy

    def run(self, request: ActionRequest, *, dry_run: bool) -> PlanResult:
        plan = self.plan(request)
        try:
            after = self._apps.patch_namespaced_deployment(
                request.target,
                request.namespace,
                plan.patch,
                dry_run="All" if dry_run else None,
                field_manager=FIELD_MANAGER,
                _content_type=_CONTENT_TYPES[plan.patch_type],
                _request_timeout=self._timeout,
            )
        except ApiException as exc:
            raise ExecutionError(_api_error("patch failed", exc)) from exc
        before_view, after_view = self._view(plan.before), self._view(after)
        return PlanResult(
            action=request,
            dry_run=dry_run,
            patch_type=plan.patch_type,
            patch=plan.patch,
            diff=_unified_diff(before_view, after_view),
            before=before_view,
            after=after_view,
            status=None if dry_run else _status(after),
        )

    def plan(self, request: ActionRequest) -> Plan:
        action = validate_action(request, self._policy)
        dep = self._get_deployment(request)
        p = action.params
        patch_type: PatchType = "strategic-merge"
        patch: dict[str, Any] | list[dict[str, Any]]
        if isinstance(p, PatchResourcesParams):
            patch = self._patch_resources(dep, p)
        elif isinstance(p, ScaleParams):
            patch = {"spec": {"replicas": p.replicas}}
        elif isinstance(p, RolloutRestartParams):
            stamp = self._clock().isoformat()
            annotations = {RESTARTED_AT_ANNOTATION: stamp}
            patch = {"spec": {"template": {"metadata": {"annotations": annotations}}}}
        elif isinstance(p, RolloutUndoParams):
            patch_type, patch = "json-patch", self._patch_undo(request, dep, p)
        else:  # pragma: no cover - exhaustive over Params
            raise ActionRejectedError(f"unsupported action {request.action_type}")
        return Plan(action=action, patch_type=patch_type, patch=patch, before=dep)

    # --- builders -----------------------------------------------------------------------

    def _patch_resources(self, dep: Any, p: PatchResourcesParams) -> dict[str, Any]:
        containers = {c.name: c for c in dep.spec.template.spec.containers}
        if p.container not in containers:
            raise ActionRejectedError(
                f"container {p.container!r} not found in deployment (has: {', '.join(containers)})"
            )
        current = containers[p.container].resources
        limits: dict[str, str] = {}
        requests: dict[str, str] = {}
        for kind in ("memory", "cpu"):
            lim, req = getattr(p, f"{kind}_limit"), getattr(p, f"{kind}_request")
            if lim is not None:
                limits[kind] = lim
            if req is not None:
                requests[kind] = req
            # Check the *effective* pair: a new limit must not drop below an existing request.
            eff_lim = lim or ((current.limits or {}).get(kind) if current else None)
            eff_req = req or ((current.requests or {}).get(kind) if current else None)
            if (
                eff_lim
                and eff_req
                and parse_quantity(eff_req, f"{kind}_request")
                > parse_quantity(eff_lim, f"{kind}_limit")
            ):
                raise ActionRejectedError(
                    f"resulting {kind} request {eff_req} would exceed the limit {eff_lim}"
                )
        resources: dict[str, dict[str, str]] = {}
        if limits:
            resources["limits"] = limits
        if requests:
            resources["requests"] = requests
        return {
            "spec": {
                "template": {
                    "spec": {"containers": [{"name": p.container, "resources": resources}]}
                }
            }
        }

    def _patch_undo(
        self, request: ActionRequest, dep: Any, p: RolloutUndoParams
    ) -> list[dict[str, Any]]:
        if not p.replicaset:
            raise ActionRejectedError("rollout_undo requires a resolved replicaset")
        try:
            rs = self._apps.read_namespaced_replica_set(
                p.replicaset, request.namespace, _request_timeout=self._timeout
            )
        except ApiException as exc:
            if exc.status == 404:
                raise ActionRejectedError(f"replicaset {p.replicaset!r} not found") from exc
            raise ExecutionError(_api_error("could not read replicaset", exc)) from exc
        owners = rs.metadata.owner_references or []
        if not any(o.kind == "Deployment" and o.uid == dep.metadata.uid for o in owners):
            raise ActionRejectedError(
                f"replicaset {p.replicaset!r} is not owned by deployment {request.target!r}"
            )
        rs_rev, dep_rev = _revision(rs), _revision(dep)
        if rs_rev <= 0 or rs_rev >= dep_rev:
            raise ActionRejectedError(
                f"replicaset {p.replicaset!r} (revision {rs_rev}) is not an earlier revision than "
                f"the current revision {dep_rev}"
            )
        if p.to_revision is not None and p.to_revision != rs_rev:
            raise ActionRejectedError(
                f"replicaset {p.replicaset!r} is revision {rs_rev}, not {p.to_revision}"
            )
        template = self._sanitize(rs.spec.template)
        template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
        return [{"op": "replace", "path": "/spec/template", "value": template}]

    # --- helpers ------------------------------------------------------------------------

    def _get_deployment(self, request: ActionRequest) -> Any:
        try:
            return self._apps.read_namespaced_deployment(
                request.target, request.namespace, _request_timeout=self._timeout
            )
        except ApiException as exc:
            if exc.status == 404:
                raise ActionRejectedError(
                    f"deployment {request.namespace}/{request.target} not found"
                ) from exc
            raise ExecutionError(_api_error("could not read deployment", exc)) from exc

    def _view(self, dep: Any) -> dict[str, Any]:
        """The parts of a deployment an action can change, as plain JSON."""
        data = self._sanitize(dep)
        spec = data.get("spec", {})
        return {"replicas": spec.get("replicas"), "template": spec.get("template", {})}


def _revision(obj: Any) -> int:
    try:
        return int((obj.metadata.annotations or {}).get(REVISION_ANNOTATION, 0))
    except (TypeError, ValueError):
        return 0


def _status(dep: Any) -> DeploymentStatus:
    s = dep.status
    return DeploymentStatus(
        generation=dep.metadata.generation,
        observed_generation=s.observed_generation if s else None,
        replicas=(s.replicas if s else 0) or 0,
        updated_replicas=(s.updated_replicas if s else 0) or 0,
        ready_replicas=(s.ready_replicas if s else 0) or 0,
        available_replicas=(s.available_replicas if s else 0) or 0,
    )


def _unified_diff(before: dict[str, Any], after: dict[str, Any]) -> str:
    a = yaml.safe_dump(before, sort_keys=True).splitlines(keepends=True)
    b = yaml.safe_dump(after, sort_keys=True).splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile="before", tofile="after"))


def _api_error(prefix: str, exc: ApiException) -> str:
    message = exc.reason or "error"
    if exc.body:
        try:
            message = json.loads(exc.body).get("message", message)
        except (ValueError, TypeError, AttributeError):
            message = str(exc.body)[:300]
    return f"{prefix}: Kubernetes API error {exc.status}: {message}"

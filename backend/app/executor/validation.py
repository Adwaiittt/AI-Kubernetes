"""Strict validation of requested actions against the executor's policy.

Anything not explicitly allowed is refused: unknown action types, unknown parameters,
namespaces outside the allowlist, quantities that don't parse or exceed configured maxima,
replica counts out of bounds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from kubernetes import utils as k8s_utils
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.executor.schemas import ActionRequest

DNS_LABEL = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
DNS_SUBDOMAIN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
# Plain decimal number with an optional k8s suffix; rejects exponents, signs, whitespace.
_QUANTITY = re.compile(r"^[0-9]+(\.[0-9]+)?(m|Ki|Mi|Gi|Ti|k|M|G|T)?$")

Label = Annotated[str, StringConstraints(pattern=DNS_LABEL, max_length=63)]
Subdomain = Annotated[str, StringConstraints(pattern=DNS_SUBDOMAIN, max_length=253)]

_parse_quantity: Any = k8s_utils.parse_quantity


class ActionRejectedError(ValueError):
    """The action is not allowed. The message is safe to show to users."""


@dataclass(frozen=True)
class ExecutorPolicy:
    allowed_namespaces: frozenset[str]
    max_memory: str = "4Gi"
    max_cpu: str = "4"
    min_replicas: int = 1
    max_replicas: int = 20


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PatchResourcesParams(_Params):
    container: Label
    memory_limit: str | None = None
    memory_request: str | None = None
    cpu_limit: str | None = None
    cpu_request: str | None = None


class RolloutRestartParams(_Params):
    pass


class RolloutUndoParams(_Params):
    to_revision: int | None = Field(default=None, ge=1)
    # Resolved by the backend (reader identity); the executor verifies it by GET and ownership.
    replicaset: Subdomain | None = None


class ScaleParams(_Params):
    replicas: int


Params = PatchResourcesParams | RolloutRestartParams | RolloutUndoParams | ScaleParams

_PARAM_MODELS: dict[str, type[_Params]] = {
    "patch_resources": PatchResourcesParams,
    "rollout_restart": RolloutRestartParams,
    "rollout_undo": RolloutUndoParams,
    "scale": ScaleParams,
}


@dataclass(frozen=True)
class ValidatedAction:
    request: ActionRequest
    params: Params


def parse_quantity(value: str, field: str) -> Decimal:
    if not _QUANTITY.match(value):
        raise ActionRejectedError(f"{field}: {value!r} is not a valid Kubernetes quantity")
    try:
        return Decimal(_parse_quantity(value))
    except (ValueError, InvalidOperation) as exc:
        raise ActionRejectedError(f"{field}: {value!r} is not a valid quantity") from exc


def validate_action(request: ActionRequest, policy: ExecutorPolicy) -> ValidatedAction:
    if request.namespace not in policy.allowed_namespaces:
        raise ActionRejectedError(
            f"namespace {request.namespace!r} is not in the executor allowlist"
        )
    if not re.match(DNS_SUBDOMAIN, request.target) or len(request.target) > 253:
        raise ActionRejectedError(f"target {request.target!r} is not a valid deployment name")

    model = _PARAM_MODELS.get(request.action_type)
    if model is None:  # pragma: no cover - ActionRequest's Literal already guarantees this
        raise ActionRejectedError(f"unsupported action_type {request.action_type!r}")
    try:
        params = model.model_validate(request.parameters)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'parameters'}: {e['msg']}"
            for e in exc.errors()
        )
        raise ActionRejectedError(
            f"invalid parameters for {request.action_type}: {problems}"
        ) from exc

    if isinstance(params, PatchResourcesParams):
        _check_resources(params, policy)
    elif isinstance(params, ScaleParams) and not (
        policy.min_replicas <= params.replicas <= policy.max_replicas
    ):
        raise ActionRejectedError(
            f"replicas must be between {policy.min_replicas} and {policy.max_replicas}, "
            f"got {params.replicas}"
        )
    return ValidatedAction(request=request, params=params)  # type: ignore[arg-type]


def _check_resources(p: PatchResourcesParams, policy: ExecutorPolicy) -> None:
    values = {
        "memory_limit": p.memory_limit,
        "memory_request": p.memory_request,
        "cpu_limit": p.cpu_limit,
        "cpu_request": p.cpu_request,
    }
    if all(v is None for v in values.values()):
        raise ActionRejectedError("patch_resources needs at least one of " + ", ".join(values))
    max_memory = parse_quantity(policy.max_memory, "max_memory")
    max_cpu = parse_quantity(policy.max_cpu, "max_cpu")
    parsed: dict[str, Decimal] = {}
    for name, value in values.items():
        if value is None:
            continue
        q = parse_quantity(value, name)
        if q <= 0:
            raise ActionRejectedError(f"{name} must be positive")
        cap = max_memory if name.startswith("memory") else max_cpu
        if q > cap:
            raise ActionRejectedError(
                f"{name} {value} exceeds the configured maximum "
                f"{policy.max_memory if name.startswith('memory') else policy.max_cpu}"
            )
        parsed[name] = q
    for kind in ("memory", "cpu"):
        req, lim = parsed.get(f"{kind}_request"), parsed.get(f"{kind}_limit")
        if req is not None and lim is not None and req > lim:
            raise ActionRejectedError(f"{kind}_request must not exceed {kind}_limit")

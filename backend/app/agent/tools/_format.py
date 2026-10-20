"""Helpers that turn kubernetes client model objects into compact, JSON-friendly values."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from kubernetes import utils as k8s_utils
from pydantic import BaseModel

# The kubernetes package is untyped; parse_quantity returns a Decimal.
parse_quantity: Callable[[str], Decimal] = k8s_utils.parse_quantity


def iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


def resources(spec: Any) -> dict[str, dict[str, str]]:
    """``{"requests": {...}, "limits": {...}}`` from a V1ResourceRequirements (or None)."""
    if spec is None:
        return {}
    out: dict[str, dict[str, str]] = {}
    if spec.requests:
        out["requests"] = dict(spec.requests)
    if spec.limits:
        out["limits"] = dict(spec.limits)
    return out


def cpu_millicores(quantity: str | None) -> float | None:
    if quantity is None:
        return None
    return round(float(parse_quantity(quantity) * Decimal(1000)), 1)


def memory_mib(quantity: str | None) -> float | None:
    if quantity is None:
        return None
    return round(float(parse_quantity(quantity) / Decimal(1024 * 1024)), 1)


class ProbeSummary(BaseModel):
    type: str
    detail: str
    initial_delay_seconds: int | None = None
    period_seconds: int | None = None
    timeout_seconds: int | None = None
    failure_threshold: int | None = None


def probe(p: Any) -> ProbeSummary | None:
    if p is None:
        return None
    if p.http_get is not None:
        kind = "httpGet"
        detail = f"{p.http_get.scheme or 'HTTP'} :{p.http_get.port}{p.http_get.path or '/'}"
    elif p.tcp_socket is not None:
        kind, detail = "tcpSocket", f":{p.tcp_socket.port}"
    elif p._exec is not None:
        kind, detail = "exec", " ".join(p._exec.command or [])
    elif getattr(p, "grpc", None) is not None:
        kind, detail = "grpc", f":{p.grpc.port}"
    else:
        kind, detail = "unknown", ""
    return ProbeSummary(
        type=kind,
        detail=detail,
        initial_delay_seconds=p.initial_delay_seconds,
        period_seconds=p.period_seconds,
        timeout_seconds=p.timeout_seconds,
        failure_threshold=p.failure_threshold,
    )


class ContainerStateSummary(BaseModel):
    state: str  # running | waiting | terminated | unknown
    reason: str | None = None
    message: str | None = None
    exit_code: int | None = None
    signal: int | None = None
    started_at: str | None = None
    finished_at: str | None = None


def container_state(state: Any) -> ContainerStateSummary | None:
    if state is None:
        return None
    if state.running is not None:
        return ContainerStateSummary(state="running", started_at=iso(state.running.started_at))
    if state.waiting is not None:
        return ContainerStateSummary(
            state="waiting", reason=state.waiting.reason, message=state.waiting.message
        )
    if state.terminated is not None:
        t = state.terminated
        return ContainerStateSummary(
            state="terminated",
            reason=t.reason,
            message=t.message,
            exit_code=t.exit_code,
            signal=t.signal,
            started_at=iso(t.started_at),
            finished_at=iso(t.finished_at),
        )
    return None

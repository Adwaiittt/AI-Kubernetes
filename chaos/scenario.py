"""Scenario definitions (scenario.yaml + manifests) and failure-state detection."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"
ActionType = Literal["patch_resources", "rollout_restart", "rollout_undo", "scale"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Step(_Strict):
    manifest: str
    wait: Literal["available"] | None = None  # wait for the Deployment to be fully available


class FailureCondition(_Strict):
    """The observable state that means the scenario has reproduced and the agent can start."""

    # crashloop: restarted >= min_restarts (at least 1) and exited non-zero. Prefer it over
    # waiting_reason=CrashLoopBackOff: recent kubelets report that reason only briefly and show
    # `terminated: Error` for most of the back-off window.
    type: Literal["terminated_reason", "waiting_reason", "unschedulable", "restarts", "crashloop"]
    reasons: list[str] = Field(default_factory=list)
    min_restarts: int = 0
    timeout_seconds: int = 180

    @model_validator(mode="after")
    def _needs_reasons(self) -> FailureCondition:
        if self.type in ("terminated_reason", "waiting_reason") and not self.reasons:
            raise ValueError(f"{self.type} needs at least one reason")
        if self.type == "restarts" and self.min_restarts < 1:
            raise ValueError("restarts needs min_restarts >= 1")
        return self


class GroundTruth(_Strict):
    category: str
    description: str
    keywords: list[list[str]] = Field(min_length=1)
    expected_actions: list[ActionType | None] = Field(min_length=1)


class Scenario(_Strict):
    id: str  # directory name, e.g. "01-oomkilled"
    title: str
    target: str
    symptom: str
    steps: list[Step] = Field(min_length=1)
    failure: FailureCondition
    ground_truth: GroundTruth

    def manifests(self, base: Path = SCENARIOS_DIR) -> list[tuple[Step, list[dict[str, Any]]]]:
        out = []
        for step in self.steps:
            text = (base / self.id / step.manifest).read_text(encoding="utf-8")
            docs = [d for d in yaml.safe_load_all(text) if d]
            out.append((step, docs))
        return out


def load_scenarios(base: Path = SCENARIOS_DIR, only: list[str] | None = None) -> list[Scenario]:
    scenarios = []
    for directory in sorted(p for p in base.iterdir() if (p / "scenario.yaml").is_file()):
        if only and not any(directory.name == o or directory.name.startswith(o) for o in only):
            continue
        raw = yaml.safe_load((directory / "scenario.yaml").read_text(encoding="utf-8"))
        scenarios.append(Scenario.model_validate({"id": directory.name, **raw}))
    if only and not scenarios:
        raise ValueError(f"no scenarios match {only}")
    return scenarios


def failure_reached(condition: FailureCondition, pods: list[Any]) -> str | None:
    """Return a human-readable description if any pod shows the failure, else None."""
    for pod in pods:
        name = pod.metadata.name
        status = pod.status
        if status is None:
            continue
        if condition.type == "unschedulable":
            for cond in status.conditions or []:
                if cond.type == "PodScheduled" and cond.status == "False":
                    return f"{name}: unschedulable ({cond.reason}: {cond.message})"
            continue
        for cs in status.container_statuses or []:
            restarts = cs.restart_count or 0
            if restarts < condition.min_restarts:
                continue
            if condition.type == "restarts":
                return f"{name}: {restarts} restarts"
            if condition.type == "crashloop":
                if restarts < max(1, condition.min_restarts):
                    continue
                waiting = cs.state.waiting if cs.state else None
                if waiting is not None and waiting.reason == "CrashLoopBackOff":
                    return f"{name}: CrashLoopBackOff (restarts={restarts})"
                for state in (cs.state, cs.last_state):
                    term = state.terminated if state else None
                    if term is not None and (term.exit_code or 0) != 0:
                        return (
                            f"{name}: crash loop, exit code {term.exit_code} (restarts={restarts})"
                        )
                continue
            states = (
                [cs.state, cs.last_state] if condition.type == "terminated_reason" else [cs.state]
            )
            for state in states:
                if state is None:
                    continue
                detail = (
                    state.terminated if condition.type == "terminated_reason" else state.waiting
                )
                if detail is not None and detail.reason in condition.reasons:
                    return f"{name}: {detail.reason} (restarts={restarts})"
    return None

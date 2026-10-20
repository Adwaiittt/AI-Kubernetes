"""Wire schemas shared by the backend and the executor service.

This package must never import ``app.agent``: the executor trusts only what is in an
:class:`ActionRequest`, re-validated by its own policy, plus a signed approval token.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ActionType = Literal["patch_resources", "rollout_restart", "rollout_undo", "scale"]
PatchType = Literal["strategic-merge", "json-patch"]


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_type: ActionType
    namespace: str
    target: str
    parameters: dict[str, Any] = Field(default_factory=dict)

    def digest(self) -> str:
        """sha256 over canonical JSON. An approval is bound to exactly this action."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class DeploymentStatus(BaseModel):
    generation: int | None = None
    observed_generation: int | None = None
    replicas: int = 0
    updated_replicas: int = 0
    ready_replicas: int = 0
    available_replicas: int = 0


class PlanResult(BaseModel):
    """Outcome of a dry run or a real apply: the exact patch and its effect."""

    action: ActionRequest
    dry_run: bool
    patch_type: PatchType
    patch: Any
    diff: str  # unified diff of the deployment's relevant fields, before -> after
    before: dict[str, Any]
    after: dict[str, Any]
    status: DeploymentStatus | None = None  # post-patch deployment status (real apply only)


class DryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: ActionRequest


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: ActionRequest
    approval_token: str = Field(min_length=1, max_length=4096)


class ExecutorError(BaseModel):
    code: str  # rejected | unauthorized | conflict | kubernetes_error
    detail: str

"""The structured final answer of an investigation, and the ``submit_report`` tool that carries it.

The model ends an investigation by calling ``submit_report`` with a :class:`RootCauseReport`. Its
input is validated here (schema) and in the loop (grounding: cited tools were actually called,
the fix targets the investigated namespace). Invalid reports go back to the model as an
``is_error`` tool result so it can correct them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ActionType = Literal["patch_resources", "rollout_restart", "rollout_undo", "scale"]
ToolName = Literal[
    "list_pods", "describe_pod", "get_pod_logs", "get_events", "get_rollout_status", "get_metrics"
]
SUBMIT_REPORT = "submit_report"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Strict):
    tool_name: ToolName = Field(description="The tool whose output supports the claim.")
    excerpt: str = Field(
        min_length=1, max_length=2000, description="The relevant snippet of that tool's output."
    )
    why_it_matters: str = Field(min_length=1, max_length=1000)


class ProposedAction(_Strict):
    action_type: ActionType
    namespace: str = Field(min_length=1, max_length=63)
    target: str = Field(min_length=1, max_length=253, description="Deployment name.")
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "patch_resources: {container, memory_limit?, memory_request?, cpu_limit?, "
            "cpu_request?}; rollout_restart: {}; rollout_undo: {to_revision?}; "
            "scale: {replicas}."
        ),
    )
    rationale: str = Field(min_length=1, max_length=2000)
    risk: str = Field(min_length=1, max_length=1000)


class RootCauseReport(_Strict):
    summary: str = Field(min_length=1, max_length=2000)
    root_cause: str = Field(min_length=1, max_length=4000)
    confidence: Literal["high", "medium", "low"]
    evidence: list[Evidence] = Field(min_length=1, max_length=20)
    proposed_fix: ProposedAction | None = None
    alternative_hypotheses: list[str] = Field(default_factory=list, max_length=10)


def submit_report_tool() -> dict[str, Any]:
    """Claude tool definition for the terminal ``submit_report`` call."""
    return {
        "name": SUBMIT_REPORT,
        "description": (
            "Submit the final root-cause report and end the investigation. Call it exactly once, "
            "when the evidence supports a conclusion or the tool budget is spent. Every evidence "
            "item must quote output from a tool you actually called. Set proposed_fix to null "
            "if none of the four allowed actions would safely fix the problem."
        ),
        "input_schema": inline_refs(RootCauseReport.model_json_schema()),
    }


def inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local ``$ref``s against ``$defs`` and drop titles, for a self-contained schema."""
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {
                k: resolve(v)
                for k, v in node.items()
                if k != "$defs" and not (k == "title" and isinstance(v, str))
            }
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    resolved: dict[str, Any] = resolve(schema)
    return resolved

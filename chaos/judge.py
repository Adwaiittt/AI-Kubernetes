"""Optional LLM-as-judge: does the report's root cause match the ground truth?

Keyword matching is cheap and deterministic but brittle; the judge catches paraphrases
("container ran out of RAM") and rejects keyword-stuffed wrong answers. It sees only the
ground-truth description and the report, never the transcript.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from chaos.scenario import GroundTruth

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "root_cause_correct": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": ["root_cause_correct", "reasoning"],
    "additionalProperties": False,
}

JUDGE_PROMPT = """\
You are grading an AI SRE agent's Kubernetes root-cause analysis against the known ground truth.

<ground_truth>
{truth}
</ground_truth>

<agent_report>
summary: {summary}
root_cause: {root_cause}
</agent_report>

Is the agent's root cause correct? It is correct if it identifies the same underlying cause as
the ground truth; wording may differ and extra detail is fine. It is incorrect if it names a
different cause, only restates the symptom, or hedges between causes without committing to the
right one. Give a one or two sentence reason."""


async def judge_root_cause(
    client: anthropic.AsyncAnthropic,
    model: str,
    truth: GroundTruth,
    report: dict[str, Any],
) -> tuple[bool, str, dict[str, int]]:
    response = await client.messages.create(
        model=model,
        max_tokens=2000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": JUDGE_PROMPT.format(
                    truth=truth.description.strip(),
                    summary=report.get("summary", ""),
                    root_cause=report.get("root_cause", ""),
                ),
            }
        ],
    )
    if response.stop_reason == "refusal":
        return False, "judge declined to grade", _usage(response)
    text = next((b.text for b in response.content if b.type == "text"), "{}")
    verdict = json.loads(text)
    return bool(verdict["root_cause_correct"]), str(verdict["reasoning"]), _usage(response)


def _usage(response: Any) -> dict[str, int]:
    u = response.usage
    return {
        "input_tokens": u.input_tokens or 0,
        "output_tokens": u.output_tokens or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }

"""Optional LLM-as-judge: does the report's root cause match the ground truth?

Keyword matching is cheap and deterministic but brittle; the judge catches paraphrases
("container ran out of RAM") and rejects keyword-stuffed wrong answers. It sees only the
ground-truth description and the report, never the transcript. Works with Claude or Gemini
(same provider as the agent), using each API's structured JSON output.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import Settings
from chaos.scenario import GroundTruth

JudgeFn = Callable[[GroundTruth, dict[str, Any]], Awaitable[tuple[bool, str]]]

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


def judge_prompt(truth: GroundTruth, report: dict[str, Any]) -> str:
    return JUDGE_PROMPT.format(
        truth=truth.description.strip(),
        summary=report.get("summary", ""),
        root_cause=report.get("root_cause", ""),
    )


def parse_verdict(text: str) -> tuple[bool, str]:
    verdict = json.loads(text)
    return bool(verdict["root_cause_correct"]), str(verdict["reasoning"])


def build_judge(settings: Settings, model: str) -> JudgeFn:
    if settings.llm_provider == "gemini":
        from google.genai import types

        from app.agent.providers.gemini import build_gemini_client

        gclient = build_gemini_client(settings)

        async def judge_gemini(truth: GroundTruth, report: dict[str, Any]) -> tuple[bool, str]:
            response = await gclient.aio.models.generate_content(
                model=model,
                contents=judge_prompt(truth, report),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=JUDGE_SCHEMA,
                    thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
                ),
            )
            if not response.text:
                return False, "judge returned no verdict"
            return parse_verdict(response.text)

        return judge_gemini

    from app.agent.providers.anthropic import build_anthropic_client

    aclient = build_anthropic_client(settings)

    async def judge_claude(truth: GroundTruth, report: dict[str, Any]) -> tuple[bool, str]:
        response = await aclient.messages.create(
            model=model,
            max_tokens=2000,
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": JUDGE_SCHEMA},
            },
            messages=[{"role": "user", "content": judge_prompt(truth, report)}],
        )
        if response.stop_reason == "refusal":
            return False, "judge declined to grade"
        text = next((b.text for b in response.content if b.type == "text"), "{}")
        return parse_verdict(text)

    return judge_claude

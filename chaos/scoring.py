"""Scoring of agent reports against ground truth, cost accounting, and result tables."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from chaos.scenario import GroundTruth, Scenario

# USD per million tokens: input, output, cache read, 5-minute cache write.
PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}


@dataclass(frozen=True)
class KeywordScore:
    passed: bool
    matched: list[str]  # the keyword that matched each group ("" if the group missed)


def keyword_score(truth: GroundTruth, report: dict[str, Any] | None) -> KeywordScore:
    """Every keyword group must be matched (any keyword in the group) in summary + root cause."""
    if not report:
        return KeywordScore(False, [""] * len(truth.keywords))
    text = f"{report.get('summary', '')}\n{report.get('root_cause', '')}".lower()
    matched = [next((k for k in group if k.lower() in text), "") for group in truth.keywords]
    return KeywordScore(all(matched), matched)


def proposed_action(report: dict[str, Any] | None) -> str | None:
    fix = (report or {}).get("proposed_fix")
    return fix.get("action_type") if fix else None


def action_matches(truth: GroundTruth, report: dict[str, Any] | None) -> bool:
    """A missing report never matches (not even a 'no fix expected' scenario)."""
    if report is None:
        return False
    return proposed_action(report) in truth.expected_actions


def cost_usd(
    model: str, usage: dict[str, int], prices: tuple[float, ...] | None = None
) -> float | None:
    rates = prices or PRICING.get(model)
    if rates is None:
        return None
    p_in, p_out, p_read, p_write = rates
    total = (
        usage.get("input_tokens", 0) * p_in
        + usage.get("output_tokens", 0) * p_out
        + usage.get("cache_read_input_tokens", 0) * p_read
        + usage.get("cache_creation_input_tokens", 0) * p_write
    )
    return round(total / 1_000_000, 6)


class RunResult(BaseModel):
    scenario: str
    run: int
    namespace: str
    status: str  # completed | failed | setup_failed
    error: str | None = None
    failure_observed: str | None = None
    setup_seconds: float = 0.0
    rca_keyword_pass: bool = False
    rca_keywords_matched: list[str] = []
    judge_pass: bool | None = None
    judge_reasoning: str | None = None
    proposed_action: str | None = None
    action_match: bool = False
    confidence: str | None = None
    tool_calls: int = 0
    model_turns: int = 0
    latency_ms: int = 0
    usage: dict[str, int] = {}
    cost_usd: float | None = None
    summary: str | None = None
    root_cause: str | None = None


def _rate(values: Sequence[bool]) -> str:
    return f"{sum(values)}/{len(values)}" if values else "–"


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def aggregate(scenarios: list[Scenario], results: list[RunResult]) -> list[dict[str, Any]]:
    rows = []
    for sc in scenarios:
        rs = [r for r in results if r.scenario == sc.id]
        ran = [r for r in rs if r.status != "setup_failed"]
        judged = [r.judge_pass for r in ran if r.judge_pass is not None]
        costs = [r.cost_usd for r in ran if r.cost_usd is not None]
        rows.append(
            {
                "scenario": sc.id,
                "title": sc.title,
                "expected": ", ".join(a or "none" for a in sc.ground_truth.expected_actions),
                "runs": len(rs),
                "setup_failed": len(rs) - len(ran),
                "rca_keyword": _rate([r.rca_keyword_pass for r in ran]),
                "rca_judge": _rate(judged) if judged else "–",
                "action_match": _rate([r.action_match for r in ran]),
                "mean_tool_calls": _mean([r.tool_calls for r in ran]),
                "median_latency_s": statistics.median([r.latency_ms / 1000 for r in ran])
                if ran
                else None,
                "mean_cost_usd": _mean(costs),
            }
        )
    return rows


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def setup_markdown(results: list[RunResult], meta: dict[str, Any]) -> str:
    lines = [
        "# Chaos scenario reproduction check (no agent runs)",
        "",
        f"- Started: {meta['started_at']}",
        "",
        "| Scenario | Run | Reproduced | Setup (s) | Observed failure |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        ok = "yes" if r.status == "completed" else f"no: {r.error}"
        lines.append(
            f"| {r.scenario} | {r.run} | {ok} | {r.setup_seconds:.1f} "
            f"| {r.failure_observed or '–'} |"
        )
    return "\n".join(lines) + "\n"


def markdown(rows: list[dict[str, Any]], results: list[RunResult], meta: dict[str, Any]) -> str:
    if meta.get("setup_only"):
        return setup_markdown(results, meta)
    lines = [
        "# Chaos benchmark results",
        "",
        f"- Model: `{meta['model']}` (effort `{meta['effort']}`)",
        f"- Runs per scenario: {meta['runs']}",
        f"- Started: {meta['started_at']}",
        f"- LLM judge: {meta['judge_model'] or 'off'}",
        "",
        "| Scenario | Expected fix | RCA (keyword) | RCA (judge) | Fix match | Tool calls "
        "| Latency p50 (s) | Cost / run |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        cost = f"${r['mean_cost_usd']:.4f}" if r["mean_cost_usd"] is not None else "–"
        lines.append(
            f"| {r['title']} | `{r['expected']}` | {r['rca_keyword']} | {r['rca_judge']} | "
            f"{r['action_match']} | {_fmt(r['mean_tool_calls'])} "
            f"| {_fmt(r['median_latency_s'])} | {cost} |"
        )
    ran = [r for r in results if r.status != "setup_failed"]
    if ran:
        total_cost = sum(r.cost_usd or 0 for r in ran)
        lines += [
            "",
            f"**Overall:** RCA {_rate([r.rca_keyword_pass for r in ran])} (keyword)"
            + (
                f", {_rate([r.judge_pass for r in ran if r.judge_pass is not None])} (judge)"
                if any(r.judge_pass is not None for r in ran)
                else ""
            )
            + f" · fix match {_rate([r.action_match for r in ran])}"
            + f" · total cost ${total_cost:.4f}",
        ]
    failures = [r for r in results if r.status != "completed"]
    if failures:
        lines += ["", "## Failed runs", ""]
        lines += [f"- `{r.scenario}` run {r.run}: {r.status}: {r.error}" for r in failures]
    return "\n".join(lines) + "\n"

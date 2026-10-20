"""Chaos benchmark: reproduce known failures on kind and score the agent against ground truth.

For each scenario and run: create a fresh namespace (with the reader Role copied in), deploy the
scenario, wait for the failure state, run a real investigation as monitor-reader, score it, and
delete the namespace. Results go to chaos/results/<timestamp>/results.{json,md}.

    # costs API tokens; ANTHROPIC_API_KEY and ANTHROPIC_MODEL from env/.env
    uv run --project backend python chaos/run_benchmark.py --runs 3
    uv run --project backend python chaos/run_benchmark.py --scenarios 01 04 --judge

    # free: only check that every scenario reproduces its failure state (no API calls)
    uv run --project backend python chaos/run_benchmark.py --setup-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # executed as a script: make `chaos` and `app` importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.agent.loop import AgentConfig, Investigator, build_anthropic_client
from app.agent.prompts import IncidentPrompt
from app.agent.tools import build_registry
from app.agent.tools.base import ToolContext
from app.core.config import Settings
from app.core.k8s import ReadOnlyClients
from chaos.cluster import Cluster, SetupError, admin_api_client, run_namespace
from chaos.judge import judge_root_cause
from chaos.scenario import Scenario, load_scenarios
from chaos.scoring import (
    RunResult,
    action_matches,
    aggregate,
    cost_usd,
    keyword_score,
    markdown,
    proposed_action,
)

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


async def run_one(
    scenario: Scenario,
    run: int,
    cluster: Cluster,
    args: argparse.Namespace,
    settings: Settings,
    agent_client: Any,
) -> RunResult:
    namespace = run_namespace(scenario)
    result = RunResult(scenario=scenario.id, run=run, namespace=namespace, status="setup_failed")
    started = time.monotonic()
    try:
        log(f"{scenario.id} #{run}: deploying into {namespace}")
        await asyncio.to_thread(cluster.create_namespace, namespace, scenario.id)
        await asyncio.to_thread(cluster.deploy, scenario, namespace)
        result.failure_observed = await asyncio.to_thread(
            cluster.wait_for_failure, scenario, namespace
        )
        result.setup_seconds = round(time.monotonic() - started, 1)
        log(f"{scenario.id} #{run}: failure reproduced ({result.failure_observed})")
        if args.setup_only:
            result.status = "completed"
            return result

        reader = ReadOnlyClients.from_api_client(
            await asyncio.to_thread(cluster.reader_api_client), settings.k8s_request_timeout_seconds
        )
        investigator = Investigator(
            agent_client,
            build_registry(),
            ToolContext(reader, settings),
            AgentConfig.from_settings(settings),
        )
        outcome = await investigator.run(
            IncidentPrompt(namespace=namespace, target=scenario.target, symptom=scenario.symptom)
        )
        report = outcome.report.model_dump(mode="json") if outcome.report else None
        kw = keyword_score(scenario.ground_truth, report)
        usage = outcome.usage.model_dump()
        result = result.model_copy(
            update={
                "status": outcome.status,
                "error": outcome.error,
                "rca_keyword_pass": kw.passed,
                "rca_keywords_matched": kw.matched,
                "proposed_action": proposed_action(report),
                "action_match": action_matches(scenario.ground_truth, report),
                "confidence": (report or {}).get("confidence"),
                "tool_calls": outcome.tool_calls,
                "model_turns": outcome.model_turns,
                "latency_ms": outcome.latency_ms,
                "usage": usage,
                "cost_usd": cost_usd(outcome.model, usage),
                "summary": (report or {}).get("summary"),
                "root_cause": (report or {}).get("root_cause"),
            }
        )
        if args.judge and report:
            passed, reasoning, _ = await judge_root_cause(
                agent_client, args.judge_model or outcome.model, scenario.ground_truth, report
            )
            result = result.model_copy(update={"judge_pass": passed, "judge_reasoning": reasoning})
        log(
            f"{scenario.id} #{run}: {result.status}"
            f" rca={'PASS' if result.rca_keyword_pass else 'FAIL'}"
            f" action={result.proposed_action} ({'match' if result.action_match else 'mismatch'})"
            f" tools={result.tool_calls} {result.latency_ms / 1000:.1f}s"
        )
        return result
    except SetupError as exc:
        result.error = str(exc)
        log(f"{scenario.id} #{run}: setup failed: {exc}")
        return result
    except Exception as exc:  # keep the benchmark going; record the failure
        result.status = "failed" if result.failure_observed else "setup_failed"
        result.error = f"{type(exc).__name__}: {exc}"
        log(f"{scenario.id} #{run}: error: {result.error}")
        return result
    finally:
        if args.keep_namespaces:
            log(f"{scenario.id} #{run}: keeping namespace {namespace}")
        else:
            await asyncio.to_thread(cluster.delete_namespace, namespace)


async def main_async(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(only=args.scenarios)
    settings = Settings()
    if args.model:
        settings = settings.model_copy(update={"anthropic_model": args.model})
    if not args.setup_only:
        AgentConfig.from_settings(settings)  # fail fast if ANTHROPIC_MODEL is missing
    agent_client = None if args.setup_only else build_anthropic_client(settings)
    cluster = Cluster(admin_api_client(args.kubeconfig, args.context))

    started_at = datetime.now(UTC)
    results: list[RunResult] = []
    for scenario in scenarios:
        for run in range(1, args.runs + 1):
            results.append(await run_one(scenario, run, cluster, args, settings, agent_client))

    rows = aggregate(scenarios, results)
    meta = {
        "model": "setup-only" if args.setup_only else settings.anthropic_model,
        "effort": settings.anthropic_effort,
        "runs": args.runs,
        "started_at": started_at.isoformat(timespec="seconds"),
        "judge_model": (args.judge_model or settings.anthropic_model) if args.judge else None,
        "setup_only": args.setup_only,
    }
    out_dir = Path(args.out) if args.out else RESULTS_DIR / started_at.strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(
            {"meta": meta, "summary": rows, "runs": [r.model_dump() for r in results]},
            indent=2, default=str,
        ) + "\n",
        encoding="utf-8", newline="\n",
    )  # fmt: skip
    md = markdown(rows, results, meta)
    (out_dir / "results.md").write_text(md, encoding="utf-8", newline="\n")
    print(md)
    log(f"wrote {out_dir}")
    leftovers = cluster.leftover_namespaces()
    if leftovers and not args.keep_namespaces:
        log(f"namespaces still terminating: {', '.join(leftovers)}")
    return 0 if all(r.status == "completed" for r in results) else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--runs", type=int, default=1, help="runs per scenario (default 1)")
    p.add_argument("--scenarios", nargs="*", help="ids or prefixes, e.g. 01 05-bad-rollout")
    p.add_argument("--model", help="override ANTHROPIC_MODEL")
    p.add_argument("--judge", action="store_true", help="also grade root causes with an LLM judge")
    p.add_argument("--judge-model", help="judge model (default: the agent model)")
    p.add_argument(
        "--setup-only", action="store_true", help="reproduce failures only; no API calls"
    )
    p.add_argument("--keep-namespaces", action="store_true", help="don't delete run namespaces")
    p.add_argument("--kubeconfig", help="admin kubeconfig for the harness (default: KUBECONFIG)")
    p.add_argument("--context", help="kubeconfig context (default: current)")
    p.add_argument("--out", help="output directory (default: chaos/results/<timestamp>)")
    args = p.parse_args(argv)
    if args.runs < 1:
        p.error("--runs must be >= 1")
    return args


def main() -> None:
    raise SystemExit(asyncio.run(main_async(parse_args())))


if __name__ == "__main__":
    main()

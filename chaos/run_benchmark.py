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

from app.agent.loop import AgentConfig, Investigator
from app.agent.prompts import IncidentPrompt
from app.agent.providers import build_provider
from app.agent.tools import build_registry
from app.agent.tools.base import ToolContext
from app.core.config import Settings
from app.core.k8s import ReadOnlyClients
from chaos.cluster import Cluster, SetupError, admin_api_client, run_namespace
from chaos.judge import JudgeFn, build_judge
from chaos.scenario import Scenario, load_scenarios
from chaos.scoring import (
    RunResult,
    action_matches,
    aggregate,
    cost_usd,
    keyword_score,
    markdown,
    proposed_action,
    scored,
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
    llm: Any,  # a ModelProvider (Claude or Gemini) or an Anthropic client
    judge: JudgeFn | None = None,
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
            llm,
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
        # An LLM API failure (quota, outage) says nothing about the agent: not scored.
        api_failed = outcome.status == "failed" and outcome.error_kind == "provider"
        result = result.model_copy(
            update={
                "status": "api_error" if api_failed else outcome.status,
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
        if args.judge and judge is not None and report:
            passed, reasoning = await judge(scenario.ground_truth, report)
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


API_ERROR_PREFIXES = (
    "Gemini API error",
    "Gemini API unreachable",
    "Claude API error",
    "Claude API unreachable",
)


def load_previous(out_dir: Path) -> tuple[list[RunResult], dict[str, Any]]:
    """Runs from an earlier results.json, with API failures (re)classified as not scored."""
    data = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
    runs = []
    for raw in data["runs"]:
        r = RunResult.model_validate(raw)
        if r.status == "failed" and (r.error or "").startswith(API_ERROR_PREFIXES):
            r = r.model_copy(update={"status": "api_error"})  # written before classification
        runs.append(r)
    meta: dict[str, Any] = data["meta"]
    return runs, meta


async def main_async(args: argparse.Namespace) -> int:
    previous: list[RunResult] = []
    prev_meta: dict[str, Any] | None = None
    if args.retry_unscored:
        previous, prev_meta = load_previous(Path(args.retry_unscored))
        if prev_meta.get("setup_only"):
            raise SystemExit("--retry-unscored needs a real benchmark run, not --setup-only")
        args.runs = prev_meta["runs"]
        args.scenarios = sorted({r.scenario for r in previous})
        args.out = args.retry_unscored
    scenarios = load_scenarios(only=args.scenarios)
    settings = Settings()
    if args.provider:
        settings = settings.model_copy(update={"llm_provider": args.provider})
    if args.model:
        field = "gemini_model" if settings.llm_provider == "gemini" else "anthropic_model"
        settings = settings.model_copy(update={field: args.model})
    llm = judge = None
    config = None
    if not args.setup_only:
        config = AgentConfig.from_settings(settings)  # fail fast on missing model / key
        llm = build_provider(settings, config)
        if args.judge:
            judge = build_judge(settings, args.judge_model or config.model)
    model_id = "setup-only" if config is None else f"{config.provider}/{config.model}"
    if prev_meta is not None and prev_meta["model"] != model_id:
        raise SystemExit(
            f"--retry-unscored: previous results are for {prev_meta['model']}, not {model_id}; "
            "use the same provider and model so the table stays comparable"
        )
    cluster = Cluster(admin_api_client(args.kubeconfig, args.context))

    started_at = datetime.now(UTC)
    if prev_meta is not None:
        results = [r for r in previous if scored(r)]
        todo = [(r.scenario, r.run) for r in previous if not scored(r)]
        by_id = {s.id: s for s in scenarios}
        log(f"retrying {len(todo)} unscored run(s), keeping {len(results)} scored")
        for scenario_id, run in todo:
            scenario = by_id[scenario_id]
            results.append(await run_one(scenario, run, cluster, args, settings, llm, judge))
        results.sort(key=lambda r: (r.scenario, r.run))
    else:
        results = []
        for scenario in scenarios:
            for run in range(1, args.runs + 1):
                results.append(await run_one(scenario, run, cluster, args, settings, llm, judge))

    rows = aggregate(scenarios, results)
    meta = {
        "model": model_id,
        "effort": settings.anthropic_effort,
        "runs": args.runs,
        "started_at": prev_meta["started_at"]
        if prev_meta
        else started_at.isoformat(timespec="seconds"),
        "judge_model": (args.judge_model or config.model) if args.judge and config else None,
        "setup_only": args.setup_only,
    }
    if prev_meta is not None:
        meta["resumed_at"] = started_at.isoformat(timespec="seconds")
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
    p.add_argument("--provider", choices=["anthropic", "gemini"], help="override LLM_PROVIDER")
    p.add_argument("--model", help="override the model (ANTHROPIC_MODEL or GEMINI_MODEL)")
    p.add_argument("--judge", action="store_true", help="also grade root causes with an LLM judge")
    p.add_argument("--judge-model", help="judge model (default: the agent model)")
    p.add_argument(
        "--setup-only", action="store_true", help="reproduce failures only; no API calls"
    )
    p.add_argument("--keep-namespaces", action="store_true", help="don't delete run namespaces")
    p.add_argument("--kubeconfig", help="admin kubeconfig for the harness (default: KUBECONFIG)")
    p.add_argument("--context", help="kubeconfig context (default: current)")
    p.add_argument("--out", help="output directory (default: chaos/results/<timestamp>)")
    p.add_argument(
        "--retry-unscored",
        metavar="RESULTS_DIR",
        help="re-run only the not-scored runs (API quota / setup failures) of an earlier run "
        "and update its results in place (useful on rate-limited free tiers)",
    )
    args = p.parse_args(argv)
    if args.runs < 1:
        p.error("--runs must be >= 1")
    return args


def main() -> None:
    raise SystemExit(asyncio.run(main_async(parse_args())))


if __name__ == "__main__":
    main()

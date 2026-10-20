"""Run one investigation from the command line (handy for manual testing against kind).

    uv run python -m app.cli --namespace demo --target checkout --symptom "pods keep restarting"

Requires ANTHROPIC_API_KEY, ANTHROPIC_MODEL and MONITOR_READER_KUBECONFIG (env or .env).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.agent.loop import AgentConfig, AgentEvent, Investigator, build_anthropic_client
from app.agent.tools import build_registry
from app.agent.tools.base import ToolContext
from app.core.config import get_settings
from app.core.k8s import build_reader_clients
from app.models.db import create_db_engine, init_db
from app.services.investigations import InvestigationService


def _print_event(_incident_id: str, event: AgentEvent) -> None:
    d = event.data
    if event.type == "tool_call":
        print(f"-> {d['tool']}({json.dumps(d['input'])})", file=sys.stderr)
    elif event.type == "tool_result":
        flag = "ERROR " if d["is_error"] else ""
        print(f"<- {flag}{d['content'][:160]}", file=sys.stderr)
    elif event.type == "model_response":
        for line in d["text"]:
            print(f"   {line}", file=sys.stderr)
    elif event.type in ("report_rejected", "nudge", "investigation_failed"):
        print(f"!! {event.type}: {json.dumps(d)}", file=sys.stderr)


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    ctx = ToolContext(build_reader_clients(settings), settings)
    investigator = Investigator(
        build_anthropic_client(settings), build_registry(), ctx, AgentConfig.from_settings(settings)
    )
    engine = create_db_engine(settings.database_url)
    init_db(engine)

    async def publish(incident_id: str, event: AgentEvent) -> None:
        _print_event(incident_id, event)

    service = InvestigationService(engine, investigator, publish)
    incident = service.create_incident(args.namespace, args.symptom, args.target)
    result = await service.run(incident.id)
    print(
        json.dumps(
            {
                "incident_id": incident.id,
                "status": result.status,
                "error": result.error,
                "tool_calls": result.tool_calls,
                "latency_ms": result.latency_ms,
                "usage": result.usage.model_dump(),
                "report": result.report.model_dump(mode="json") if result.report else None,
            },
            indent=2,
        )
    )
    return 0 if result.status == "completed" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one AI SRE investigation.")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--symptom", required=True)
    parser.add_argument("--target")
    raise SystemExit(asyncio.run(_main(parser.parse_args())))


if __name__ == "__main__":
    main()

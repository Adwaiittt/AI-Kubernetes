"""The chaos benchmark harness: scenario definitions, failure detection, scoring, run flow."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import pytest
from anthropic.types.beta import BetaMessage
from chaos import run_benchmark
from chaos.cluster import SetupError, reader_rbac_docs, run_namespace
from chaos.scenario import FailureCondition, GroundTruth, Scenario, failure_reached, load_scenarios
from chaos.scoring import (
    PRICING,
    RunResult,
    action_matches,
    aggregate,
    cost_usd,
    keyword_score,
    markdown,
)
from kubernetes import client as k

from app.agent.report import SUBMIT_REPORT
from app.core.config import Settings
from app.core.k8s import ReadOnlyClients
from tests.conftest import FakeCluster
from tests.unit import factories as f
from tests.unit.agent_fakes import MODEL, fake_client, message, report_input, tool_use

SCENARIOS = load_scenarios()


# --- scenario definitions -------------------------------------------------------------------


def test_the_six_required_scenarios_exist() -> None:
    assert [s.id for s in SCENARIOS] == [
        "01-oomkilled",
        "02-missing-env",
        "03-liveness-probe",
        "04-image-pull",
        "05-bad-rollout",
        "06-pending-pod",
    ]
    expected = {s.id: s.ground_truth.expected_actions for s in SCENARIOS}
    assert expected["05-bad-rollout"] == ["rollout_undo"]
    assert expected["01-oomkilled"] == ["patch_resources"]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_manifests_are_namespace_agnostic_deployments_of_the_target(scenario: Scenario) -> None:
    for _step, docs in scenario.manifests():
        assert docs
        for doc in docs:
            assert doc["kind"] == "Deployment"
            assert "namespace" not in doc["metadata"], "the runner assigns a fresh namespace"
            assert doc["metadata"]["name"] == scenario.target
    first = scenario.manifests()[0][1][0]
    assert first["spec"]["selector"]["matchLabels"] == {"app": scenario.target}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_ground_truth_is_self_consistent(scenario: Scenario) -> None:
    """A report that restates the ground-truth description must pass the keyword check."""
    truth = scenario.ground_truth
    score = keyword_score(truth, {"summary": "", "root_cause": truth.description})
    assert score.passed, f"keywords {truth.keywords} don't match the description"


def test_load_scenarios_filter() -> None:
    assert [s.id for s in load_scenarios(only=["04", "06-pending"])] == [
        "04-image-pull",
        "06-pending-pod",
    ]
    with pytest.raises(ValueError, match="no scenarios"):
        load_scenarios(only=["99"])


def test_failure_condition_validation() -> None:
    with pytest.raises(ValueError):
        FailureCondition(type="waiting_reason")
    with pytest.raises(ValueError):
        FailureCondition(type="restarts")


def test_reader_rbac_is_retargeted_and_still_read_only() -> None:
    docs = reader_rbac_docs("chaos-x-123")
    assert {d["kind"] for d in docs} == {"Role", "RoleBinding"}
    assert all(d["metadata"]["namespace"] == "chaos-x-123" for d in docs)
    role = next(d for d in docs if d["kind"] == "Role")
    assert {v for rule in role["rules"] for v in rule["verbs"]} <= {"get", "list", "watch"}


def test_run_namespace_is_unique_and_valid() -> None:
    names = {run_namespace(SCENARIOS[0]) for _ in range(20)}
    assert len(names) == 20
    assert all(n.startswith("chaos-oomkilled-") and len(n) <= 63 for n in names)


# --- failure detection ----------------------------------------------------------------------


def _crashing(waiting: str | None, exit_code: int = 1, restarts: int = 2) -> Any:
    status = f.oom_status(restarts=restarts)
    status.state = k.V1ContainerState(
        waiting=k.V1ContainerStateWaiting(reason=waiting) if waiting else None,
        terminated=None
        if waiting
        else k.V1ContainerStateTerminated(reason="Error", exit_code=exit_code),
    )
    status.last_state = k.V1ContainerState(
        terminated=k.V1ContainerStateTerminated(reason="Error", exit_code=exit_code)
    )
    return f.pod("p", statuses=[status])


def test_crashloop_detects_both_kubelet_representations() -> None:
    cond = FailureCondition(type="crashloop", min_restarts=1)
    assert (
        failure_reached(cond, [_crashing("CrashLoopBackOff")]) == "p: CrashLoopBackOff (restarts=2)"
    )
    # Newer kubelets show terminated/Error for most of the back-off window.
    assert "exit code 1" in (failure_reached(cond, [_crashing(None)]) or "")
    assert failure_reached(cond, [_crashing(None, restarts=0)]) is None
    assert failure_reached(cond, [f.pod("ok", statuses=[f.running_status()])]) is None


def test_terminated_reason_checks_last_state() -> None:
    cond = FailureCondition(type="terminated_reason", reasons=["OOMKilled"], min_restarts=1)
    assert failure_reached(cond, [f.pod("p", statuses=[f.oom_status()])]) == (
        "p: OOMKilled (restarts=4)"
    )
    assert failure_reached(cond, [f.pod("p", statuses=[f.running_status()])]) is None


def test_waiting_reason_and_unschedulable_and_restarts() -> None:
    pull = f.running_status()
    pull.state = k.V1ContainerState(waiting=k.V1ContainerStateWaiting(reason="ImagePullBackOff"))
    cond = FailureCondition(type="waiting_reason", reasons=["ImagePullBackOff", "ErrImagePull"])
    assert (
        failure_reached(cond, [f.pod("p", statuses=[pull])]) == "p: ImagePullBackOff (restarts=0)"
    )

    pending = f.pod("p", phase="Pending", conditions=[
        k.V1PodCondition(type="PodScheduled", status="False", reason="Unschedulable",
                         message="0/2 nodes are available: Insufficient memory"),
    ])  # fmt: skip
    found = failure_reached(FailureCondition(type="unschedulable"), [pending])
    assert found is not None and "Insufficient memory" in found

    restarts = FailureCondition(type="restarts", min_restarts=2)
    assert failure_reached(restarts, [f.pod("p", statuses=[f.oom_status(restarts=3)])])
    assert failure_reached(restarts, [f.pod("p", statuses=[f.oom_status(restarts=1)])]) is None
    assert failure_reached(restarts, [k.V1Pod(metadata=k.V1ObjectMeta(name="x"))]) is None


# --- scoring --------------------------------------------------------------------------------

TRUTH = GroundTruth(
    category="resource_limits",
    description="d",
    keywords=[["oom", "out of memory"], ["limit"]],
    expected_actions=["patch_resources"],
)
NO_FIX = GroundTruth(category="c", description="d", keywords=[["x"]], expected_actions=[None])


def test_keyword_score() -> None:
    hit = keyword_score(TRUTH, {"summary": "Pod OOMKilled", "root_cause": "memory LIMIT too low"})
    assert hit.passed and hit.matched == ["oom", "limit"]
    miss = keyword_score(TRUTH, {"summary": "Out of memory", "root_cause": "a leak"})
    assert not miss.passed and miss.matched == ["out of memory", ""]
    assert not keyword_score(TRUTH, None).passed


def test_action_matching_including_no_fix() -> None:
    assert action_matches(TRUTH, {"proposed_fix": {"action_type": "patch_resources"}})
    assert not action_matches(TRUTH, {"proposed_fix": {"action_type": "scale"}})
    assert not action_matches(TRUTH, {"proposed_fix": None})
    assert action_matches(NO_FIX, {"proposed_fix": None})
    assert not action_matches(NO_FIX, {"proposed_fix": {"action_type": "rollout_restart"}})
    assert not action_matches(NO_FIX, None)  # no report is never a match


def test_cost() -> None:
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 100_000,
        "cache_read_input_tokens": 2_000_000,
        "cache_creation_input_tokens": 400_000,
    }
    # 2.00 + 1.00 + 0.40 + 1.00
    assert cost_usd("claude-sonnet-5-5", usage) == pytest.approx(4.40)
    assert cost_usd("unknown-model", usage) is None
    assert cost_usd("unknown-model", usage, prices=(1, 1, 1, 1)) == pytest.approx(3.5)
    assert set(PRICING) >= {"claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"}


def test_aggregate_and_markdown() -> None:
    sc = SCENARIOS[:2]
    results = [
        RunResult(scenario=sc[0].id, run=1, namespace="n1", status="completed",
                  rca_keyword_pass=True, action_match=True, tool_calls=5, latency_ms=20_000,
                  cost_usd=0.05, judge_pass=True),
        RunResult(scenario=sc[0].id, run=2, namespace="n2", status="completed",
                  rca_keyword_pass=False, action_match=True, tool_calls=7, latency_ms=30_000,
                  cost_usd=0.07, judge_pass=True),
        RunResult(scenario=sc[1].id, run=1, namespace="n3", status="setup_failed", error="boom"),
    ]  # fmt: skip
    rows = aggregate(sc, results)
    assert rows[0]["rca_keyword"] == "1/2" and rows[0]["rca_judge"] == "2/2"
    assert rows[0]["mean_tool_calls"] == 6 and rows[0]["median_latency_s"] == 25
    assert rows[0]["mean_cost_usd"] == pytest.approx(0.06)
    assert rows[1]["setup_failed"] == 1 and rows[1]["rca_keyword"] == "–"
    md = markdown(
        rows,
        results,
        {"model": MODEL, "effort": "medium", "runs": 2, "started_at": "t", "judge_model": MODEL},
    )
    assert (
        "| OOMKilled (memory limit too low for the workload) | `patch_resources` | 1/2 | 2/2 |"
        in md
    )
    assert "total cost $0.1200" in md
    assert "`02-missing-env` run 1: setup_failed: boom" in md
    setup = markdown(rows, results, {"setup_only": True, "started_at": "t"})
    assert "| 02-missing-env | 1 | no: boom |" in setup


# --- run flow (no cluster, scripted agent) --------------------------------------------------


class StubCluster:
    def __init__(self, fail_setup: bool = False) -> None:
        self.fail_setup = fail_setup
        self.created: list[str] = []
        self.deleted: list[str] = []

    def create_namespace(self, name: str, scenario_id: str) -> None:
        self.created.append(name)

    def deploy(self, scenario: Scenario, namespace: str) -> None:
        if self.fail_setup:
            raise SetupError("deployment never became available")

    def wait_for_failure(self, scenario: Scenario, namespace: str) -> str:
        return "pod-1: OOMKilled (restarts=1)"

    def reader_api_client(self) -> object:
        return object()

    def delete_namespace(self, name: str) -> None:
        self.deleted.append(name)


def _args(**kw: Any) -> argparse.Namespace:
    defaults = {"setup_only": False, "judge": False, "judge_model": None, "keep_namespaces": False}
    return argparse.Namespace(**{**defaults, **kw})


def _run(
    cluster: StubCluster, fake: FakeCluster, *responses: BetaMessage, monkeypatch: Any, **kw: Any
) -> RunResult:
    monkeypatch.setattr(ReadOnlyClients, "from_api_client", lambda *_a, **_k: fake.ctx.clients)
    client, _ = fake_client(*responses)
    settings = Settings(_env_file=None, ANTHROPIC_MODEL=MODEL)
    stub: Any = cluster  # duck-typed stand-in for chaos.cluster.Cluster
    return asyncio.run(run_benchmark.run_one(SCENARIOS[0], 1, stub, _args(**kw), settings, client))


def test_run_one_records_a_failed_investigation(cluster: FakeCluster, monkeypatch: Any) -> None:
    cluster.core.list_namespaced_pod.return_value = f.pod_list(f.pod("checkout-1"))
    stub = StubCluster()
    wrong_ns = report_input(tools=("list_pods",), namespace="somewhere-else")
    result = _run(
        stub, cluster,
        message(tool_use("list_pods", {"namespace": "x"})),
        message(tool_use(SUBMIT_REPORT, wrong_ns)),
        message(tool_use(SUBMIT_REPORT, wrong_ns)),
        monkeypatch=monkeypatch,
    )  # fmt: skip
    # The fix targets a namespace other than the run's -> rejected twice -> failed run, which
    # scores as a miss, and the namespace is still cleaned up.
    assert result.status == "failed"
    assert result.error == "report failed validation 2 times"
    assert not result.rca_keyword_pass and not result.action_match
    assert stub.deleted == stub.created


def test_run_one_happy_path(cluster: FakeCluster, monkeypatch: Any) -> None:
    cluster.core.list_namespaced_pod.return_value = f.pod_list(f.pod("checkout-1"))
    stub = StubCluster()
    monkeypatch.setattr(run_benchmark, "run_namespace", lambda _s: "chaos-oomkilled-abc123")
    result = _run(
        stub, cluster,
        message(tool_use("list_pods", {"namespace": "chaos-oomkilled-abc123"}),
                input_tokens=10_000),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",),
                                                     namespace="chaos-oomkilled-abc123")),
                output_tokens=2_000),
        monkeypatch=monkeypatch,
    )  # fmt: skip
    assert result.status == "completed"
    assert result.rca_keyword_pass is True
    assert result.proposed_action == "patch_resources" and result.action_match is True
    assert result.tool_calls == 1 and result.model_turns == 2
    # 2 turns: input 1_000 + 10_000, output 2_000 + 200 (the fakes' defaults fill the rest).
    assert result.usage["input_tokens"] == 11_000 and result.usage["output_tokens"] == 2_200
    assert result.cost_usd == pytest.approx((11_000 * 2.0 + 2_200 * 10.0) / 1_000_000)
    assert result.failure_observed == "pod-1: OOMKilled (restarts=1)"
    assert stub.deleted == ["chaos-oomkilled-abc123"]


def test_run_one_setup_failure_is_recorded_and_cleaned_up(
    cluster: FakeCluster, monkeypatch: Any
) -> None:
    stub = StubCluster(fail_setup=True)
    result = _run(stub, cluster, monkeypatch=monkeypatch)
    assert result.status == "setup_failed"
    assert result.error == "deployment never became available"
    assert stub.deleted == stub.created and len(stub.created) == 1


def test_run_one_setup_only_never_calls_the_model(cluster: FakeCluster, monkeypatch: Any) -> None:
    stub = StubCluster()
    result = _run(stub, cluster, monkeypatch=monkeypatch, setup_only=True)  # no responses scripted
    assert result.status == "completed" and result.tool_calls == 0
    assert stub.deleted == stub.created


def test_run_one_with_judge(cluster: FakeCluster, monkeypatch: Any) -> None:
    cluster.core.list_namespaced_pod.return_value = f.pod_list(f.pod("checkout-1"))
    monkeypatch.setattr(run_benchmark, "run_namespace", lambda _s: "ns-j")

    async def fake_judge(*_a: Any, **_k: Any) -> tuple[bool, str, dict[str, int]]:
        return True, "names the 32Mi limit as the cause", {}

    monkeypatch.setattr(run_benchmark, "judge_root_cause", fake_judge)
    result = _run(
        StubCluster(), cluster,
        message(tool_use("list_pods", {"namespace": "ns-j"})),
        message(tool_use(SUBMIT_REPORT, report_input(tools=("list_pods",), namespace="ns-j"))),
        monkeypatch=monkeypatch, judge=True,
    )  # fmt: skip
    assert result.judge_pass is True and "32Mi" in (result.judge_reasoning or "")


def test_cli_args() -> None:
    args = run_benchmark.parse_args(["--runs", "3", "--scenarios", "01", "04", "--judge"])
    assert args.runs == 3 and args.scenarios == ["01", "04"] and args.judge
    with pytest.raises(SystemExit):
        run_benchmark.parse_args(["--runs", "0"])

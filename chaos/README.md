# Chaos benchmark

Reproducible Kubernetes failures with a ground-truth root cause, used to measure the agent.

| # | Scenario | Failure the harness waits for | Expected fix |
|---|---|---|---|
| 01 | OOMKilled: 32Mi limit, ~40MB working set | last termination `OOMKilled` | `patch_resources` |
| 02 | CrashLoopBackOff: missing `DATABASE_URL` | crash loop (restarts, non-zero exit) | none: config change |
| 03 | CrashLoopBackOff: liveness probe on wrong port | ≥ 2 restarts | none: probe change |
| 04 | ImagePullBackOff: bad tag in a new rollout | `ErrImagePull` / `ImagePullBackOff` | `rollout_undo` |
| 05 | Bad rollout: revision 2 crashes, revision 1 healthy | crash loop | `rollout_undo` |
| 06 | Pending: requests 512Gi memory | `Unschedulable` | `patch_resources` |

"None" scenarios check that the agent does **not** propose an automated action when none of the
four allowed actions can fix the problem.

## Run

Run from `backend/` so the LLM settings in `backend/.env` are picked up (`LLM_PROVIDER`,
`GEMINI_API_KEY`/`GEMINI_MODEL` or `ANTHROPIC_API_KEY`/`ANTHROPIC_MODEL`):

```bash
cd backend
# Free: check every scenario reproduces its failure state (no API calls)
uv run python ../chaos/run_benchmark.py --setup-only

# Real benchmark (uses API quota / tokens)
uv run python ../chaos/run_benchmark.py --runs 3 --judge
uv run python ../chaos/run_benchmark.py --scenarios 01 05 --provider gemini --model gemini-3.8-flash

# Rate-limited (e.g. a free tier)? Re-run only the runs that hit the quota, in place:
uv run python ../chaos/run_benchmark.py --judge --retry-unscored ../chaos/results/<timestamp>
```

Needs the kind cluster from `deploy/scripts/setup-kind.sh` and an admin kubeconfig context for
the harness (it creates namespaces). The **agent** never uses that: each run mints a short-lived
`monitor-reader` token, and the reader Role is copied into the run's namespace only.

## Per run

1. Create `chaos-<scenario>-<random>` and copy in the reader Role/RoleBinding.
2. Apply the scenario's steps (two-revision scenarios wait for revision 1 to be available).
3. Wait for the scenario's failure condition.
4. Run a real investigation as `monitor-reader` with the incident's symptom text.
5. Score, then delete the namespace (always, unless `--keep-namespaces`).

## Scoring

- **RCA (keyword):** every keyword group in `scenario.yaml` must appear (any keyword from each
  group) in the report's summary + root cause. Deterministic and free, but brittle.
- **RCA (judge),** with `--judge`: an LLM grades the root cause against the ground-truth
  description, catching paraphrases and keyword-stuffed wrong answers.
- **Fix match:** the proposed `action_type` (or no fix) is in `expected_actions`.
- **Tool calls, latency, cost:** from the agent's own accounting. Cost uses the per-model
  paid-tier prices in `scoring.py` (USD per MTok: input, output, cache read, cache write).
- **Not scored:** runs where the setup failed or the **LLM API** itself failed (quota, outage)
  measure nothing about the agent. They're excluded from the rates and listed separately. Agent
  failures (refusals, invalid reports, hitting the tool cap) still count as misses.

Results go to `chaos/results/<timestamp>/results.{md,json}`; the JSON has every run's report
summary, root cause and the matched keywords.

## Adding a scenario

Create `scenarios/NN-name/` with Deployment manifests (no `namespace:`; the runner assigns one)
and a `scenario.yaml`. Unit tests check every scenario: manifests are namespace-agnostic
Deployments of the target, and the ground-truth keywords match the ground-truth description.

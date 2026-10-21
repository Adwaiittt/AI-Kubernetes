# Chaos benchmark results

- Model: `gemini/gemini-3.5-flash-lite` (effort `medium`)
- Runs per scenario: 1
- Started: 2026-10-21T05:51:48+00:00
- LLM judge: gemini-3.5-flash-lite

| Scenario | Expected fix | Scored runs | RCA (keyword) | RCA (judge) | Fix match | Tool calls | Latency p50 (s) | Cost / run |
|---|---|---|---|---|---|---|---|---|
| OOMKilled (memory limit too low for the workload) | `patch_resources` | 1/1 | 1/1 | 1/1 | 1/1 | 5.0 | 8.0 | $0.0072 |
| CrashLoopBackOff (app exits on a missing required env var) | `none` | 1/1 | 1/1 | 1/1 | 1/1 | 5.0 | 10.1 | $0.0085 |
| CrashLoopBackOff (liveness probe checks the wrong port) | `none` | 1/1 | 1/1 | 1/1 | 1/1 | 5.0 | 6.8 | $0.0068 |
| ImagePullBackOff (bad image tag introduced by a new rollout) | `rollout_undo` | 1/1 | 1/1 | 1/1 | 1/1 | 3.0 | 3.8 | $0.0039 |
| Bad rollout (new revision crashes, previous revision healthy) | `rollout_undo` | 1/1 | 1/1 | 1/1 | 1/1 | 5.0 | 6.5 | $0.0063 |
| Pending pod (resource requests exceed node capacity) | `patch_resources` | 1/1 | 1/1 | 1/1 | 1/1 | 5.0 | 8.8 | $0.0082 |

**Overall:** RCA 6/6 (keyword), 6/6 (judge) · fix match 6/6 · total cost $0.0410

# Chaos benchmark results

- Model: `gemini/gemini-3.8-flash` (effort `medium`)
- Runs per scenario: 1
- Started: 2026-10-21T05:04:29+00:00
- LLM judge: gemini-3.8-flash

| Scenario | Expected fix | Scored runs | RCA (keyword) | RCA (judge) | Fix match | Tool calls | Latency p50 (s) | Cost / run |
|---|---|---|---|---|---|---|---|---|
| OOMKilled (memory limit too low for the workload) | `patch_resources` | 1/1 | 1/1 | 1/1 | 1/1 | 7.0 | 38.0 | $0.0284 |
| CrashLoopBackOff (app exits on a missing required env var) | `none` | 1/1 | 1/1 | 1/1 | 1/1 | 7.0 | 32.2 | $0.0186 |
| CrashLoopBackOff (liveness probe checks the wrong port) | `none` | 1/1 | 1/1 | 1/1 | 1/1 | 7.0 | 36.2 | $0.0237 |
| ImagePullBackOff (bad image tag introduced by a new rollout) | `rollout_undo` | 0/1 | – | – | – | – | – | – |
| Bad rollout (new revision crashes, previous revision healthy) | `rollout_undo` | 0/1 | – | – | – | – | – | – |
| Pending pod (resource requests exceed node capacity) | `patch_resources` | 0/1 | – | – | – | – | – | – |

**Overall:** RCA 3/3 (keyword), 3/3 (judge) · fix match 3/3 · total cost $0.0707

## Not scored (setup or LLM API failures; no measurement of the agent)

- `04-image-pull` run 1: api_error: Gemini API error 429: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To mo
- `05-bad-rollout` run 1: api_error: Gemini API error 429: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To mo
- `06-pending-pod` run 1: api_error: Gemini API error 429: You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. To mo

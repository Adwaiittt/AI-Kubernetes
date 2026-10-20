# Chaos scenario reproduction check (no agent runs)

- Started: 2026-10-20T17:56:54+00:00

| Scenario | Run | Reproduced | Setup (s) | Observed failure |
|---|---|---|---|---|
| 01-oomkilled | 1 | yes | 3.1 | checkout-788bd9c6c5-rc7mx: OOMKilled (restarts=1) |
| 02-missing-env | 1 | yes | 3.1 | orders-84fb66d974-rc6jx: crash loop, exit code 1 (restarts=1) |
| 03-liveness-probe | 1 | yes | 75.3 | payments-78fd4648cb-zbtjr: 2 restarts |
| 04-image-pull | 1 | yes | 5.1 | catalog-555b6bf574-95qjx: ErrImagePull (restarts=0) |
| 05-bad-rollout | 1 | yes | 5.1 | inventory-77bc49dd6b-djt6d: crash loop, exit code 2 (restarts=1) |
| 06-pending-pod | 1 | yes | 3.1 | analytics-cdd874c78-nmnjd: unschedulable (Unschedulable: 0/2 nodes are available: 1 Insufficient memory, 1 node(s) had untolerated taint(s). preemption: 0/2 nodes are available: 2 Preemption is not helpful for scheduling.) |

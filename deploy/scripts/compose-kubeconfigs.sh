#!/usr/bin/env bash
# Kubeconfigs for docker-compose: same per-ServiceAccount tokens, but pointing at the kind
# control plane by its name on the `kind` docker network (covered by kind's API server cert).
set -euo pipefail
cd "$(dirname "$0")/../.."
SERVER="https://${KIND_CLUSTER:-k8s-ai-monitor}-control-plane:6443"
for sa in monitor-reader monitor-executor; do
  KUBE_SERVER="$SERVER" deploy/scripts/make-kubeconfig.sh "$sa" ".kube/compose/$sa.kubeconfig"
done

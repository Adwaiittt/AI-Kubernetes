#!/usr/bin/env bash
# One-shot local cluster: kind + RBAC manifests + metrics-server + per-identity kubeconfigs.
# Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/../.."

METRICS_SERVER_VERSION="v0.7.2"
CLUSTER="k8s-ai-monitor"

if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --config deploy/kind-config.yaml
fi
kubectl config use-context "kind-$CLUSTER"

kubectl apply -f deploy/k8s/00-namespaces.yaml \
  -f deploy/k8s/10-reader-rbac.yaml -f deploy/k8s/20-executor-rbac.yaml

# metrics-server; kind kubelets use self-signed certs, hence --kubelet-insecure-tls (local only).
kubectl apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/${METRICS_SERVER_VERSION}/components.yaml"
if ! kubectl -n kube-system get deployment metrics-server -o jsonpath='{.spec.template.spec.containers[0].args}' | grep -q kubelet-insecure-tls; then
  kubectl -n kube-system patch deployment metrics-server --type=json \
    -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
fi
kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s

deploy/scripts/make-kubeconfig.sh monitor-reader .kube/monitor-reader.kubeconfig
deploy/scripts/make-kubeconfig.sh monitor-executor .kube/monitor-executor.kubeconfig
echo "export MONITOR_READER_KUBECONFIG=$(pwd)/.kube/monitor-reader.kubeconfig"
echo "export EXECUTOR_KUBECONFIG=$(pwd)/.kube/monitor-executor.kubeconfig"

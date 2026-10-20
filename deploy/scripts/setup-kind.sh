#!/usr/bin/env bash
# One-shot local cluster: kind + RBAC manifests + metrics-server + reader kubeconfig.
set -euo pipefail
cd "$(dirname "$0")/../.."

METRICS_SERVER_VERSION="v0.7.2"
CLUSTER="k8s-ai-monitor"

if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --config deploy/kind-config.yaml
fi
kubectl config use-context "kind-$CLUSTER"

kubectl apply -f deploy/k8s/00-namespaces.yaml -f deploy/k8s/10-reader-rbac.yaml

# metrics-server; kind kubelets use self-signed certs, hence --kubelet-insecure-tls (local only).
kubectl apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/${METRICS_SERVER_VERSION}/components.yaml"
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' || true
kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s

deploy/scripts/make-kubeconfig.sh monitor-reader .kube/monitor-reader.kubeconfig
echo "export MONITOR_READER_KUBECONFIG=$(pwd)/.kube/monitor-reader.kubeconfig"

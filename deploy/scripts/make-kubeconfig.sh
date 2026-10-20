#!/usr/bin/env bash
# Mint a short-lived kubeconfig that authenticates AS a monitor ServiceAccount (TokenRequest API),
# so local processes run with exactly the RBAC the in-cluster pods get, not your admin context.
#
#   deploy/scripts/make-kubeconfig.sh [service-account] [output-file]
#   deploy/scripts/make-kubeconfig.sh monitor-reader .kube/monitor-reader.kubeconfig
set -euo pipefail

SA="${1:-monitor-reader}"
OUT="${2:-.kube/${SA}.kubeconfig}"
NS="k8s-ai-monitor"
DURATION="${TOKEN_DURATION:-24h}"

TOKEN="$(kubectl create token "$SA" -n "$NS" --duration="$DURATION")"
SERVER="$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.server}')"
CA="$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"

mkdir -p "$(dirname "$OUT")"
umask 077
cat > "$OUT" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: k8s-ai-monitor
    cluster:
      server: ${SERVER}
      certificate-authority-data: ${CA}
users:
  - name: ${SA}
    user:
      token: ${TOKEN}
contexts:
  - name: ${SA}
    context:
      cluster: k8s-ai-monitor
      user: ${SA}
current-context: ${SA}
EOF
echo "wrote $OUT (valid for $DURATION)"

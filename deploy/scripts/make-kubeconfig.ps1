# Mint a short-lived kubeconfig that authenticates AS a monitor ServiceAccount (TokenRequest API).
#   .\deploy\scripts\make-kubeconfig.ps1 [-ServiceAccount monitor-reader] [-Out .kube\monitor-reader.kubeconfig]
param(
    [string]$ServiceAccount = "monitor-reader",
    [string]$Out = "",
    [string]$Duration = "24h"
)
$ErrorActionPreference = "Stop"
$Namespace = "k8s-ai-monitor"
if (-not $Out) { $Out = ".kube\$ServiceAccount.kubeconfig" }

$token = kubectl create token $ServiceAccount -n $Namespace --duration=$Duration
if ($LASTEXITCODE -ne 0) { throw "kubectl create token failed" }
$server = kubectl config view --minify --raw -o "jsonpath={.clusters[0].cluster.server}"
$ca = kubectl config view --minify --raw -o "jsonpath={.clusters[0].cluster.certificate-authority-data}"

New-Item -ItemType Directory -Force (Split-Path $Out -Parent) | Out-Null
@"
apiVersion: v1
kind: Config
clusters:
  - name: k8s-ai-monitor
    cluster:
      server: $server
      certificate-authority-data: $ca
users:
  - name: $ServiceAccount
    user:
      token: $token
contexts:
  - name: $ServiceAccount
    context:
      cluster: k8s-ai-monitor
      user: $ServiceAccount
current-context: $ServiceAccount
"@ | Set-Content -Encoding utf8 -NoNewline $Out
Write-Host "wrote $Out (valid for $Duration)"

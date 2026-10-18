#!/usr/bin/env bash
# End-to-end demo on a local kind cluster: ARC + Kagent + arc-intel.
# Needs: kind, kubectl, helm, docker, and env ANTHROPIC_API_KEY, GITHUB_APP_ID,
# GITHUB_APP_INSTALLATION_ID, GITHUB_APP_PRIVATE_KEY_FILE. Edit githubConfigUrl in demo/values first.
set -euo pipefail
cd "$(dirname "$0")/../.."

ARC_VERSION="${ARC_VERSION:-0.13.0}"
IMAGE="ghcr.io/my-org/arc-intel-mcp:0.1.0"

kind get clusters | grep -qx arc-intel || kind create cluster --name arc-intel

echo "==> ARC controller ${ARC_VERSION}"
helm upgrade --install arc oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller \
  -n arc-systems --create-namespace --version "$ARC_VERSION" --wait

echo "==> Runner namespaces, GitHub App secret, NetworkPolicy"
kubectl create ns arc-runners --dry-run=client -o yaml | kubectl apply -f -
kubectl -n arc-runners create secret generic arc-github-app \
  --from-literal=github_app_id="$GITHUB_APP_ID" \
  --from-literal=github_app_installation_id="$GITHUB_APP_INSTALLATION_ID" \
  --from-file=github_app_private_key="$GITHUB_APP_PRIVATE_KEY_FILE" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: runners-egress, namespace: arc-runners}
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - {}   # replace with explicit GitHub / registry CIDRs in production
EOF

echo "==> Scale sets"
for name in linux-standard linux-large; do
  helm upgrade --install "$name" oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set \
    -n arc-runners --version "$ARC_VERSION" -f demo/values/linux-standard.yaml \
    $([ "$name" = linux-large ] && echo "--set maxRunners=30")
done
helm upgrade --install legacy-build oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set \
  -n arc-runners-legacy --create-namespace --version "$ARC_VERSION" -f demo/values/legacy-build.yaml

echo "==> Kagent"
helm upgrade --install kagent-crds oci://ghcr.io/kagent-dev/kagent/helm/kagent-crds -n kagent --create-namespace
helm upgrade --install kagent oci://ghcr.io/kagent-dev/kagent/helm/kagent -n kagent \
  --set providers.default=anthropic --set providers.anthropic.apiKey="$ANTHROPIC_API_KEY" --wait
kubectl -n kagent create secret generic kagent-anthropic --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "==> arc-intel MCP server"
docker build -t "$IMAGE" mcp-server
kind load docker-image "$IMAGE" --name arc-intel
kubectl apply -k deploy/mcp-server
kubectl -n arc-intel rollout status deploy/arc-intel-mcp --timeout=120s

echo "==> Agent"
kubectl apply -k deploy/kagent
kubectl apply -k deploy/digest
kubectl -n kagent wait --for=condition=Ready agent/arc-compliance-agent --timeout=180s || true

cat <<'EOF'

Ready. Next:
  demo/scripts/simulate-drift.sh          # make linux-large drift by hand
  kagent dashboard                        # chat with arc-compliance-agent
  kagent invoke -n kagent --agent arc-compliance-agent --task "How compliant is our ARC fleet?"
EOF

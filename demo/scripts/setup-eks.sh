#!/usr/bin/env bash
# ARC + Kagent + arc-intel on Amazon EKS.
# Needs: aws, eksctl, kubectl, kustomize, helm, docker, envsubst, and:
#   AWS_ACCOUNT_ID, AWS_REGION, CLUSTER_NAME, BEDROCK_MODEL_ID (Claude inference profile id),
#   AMP_WORKSPACE_ID (optional), and a Secrets Manager secret "arc/github-app" holding
#   {"github_app_id": "...", "github_app_installation_id": "...", "github_app_private_key": "..."}.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${AWS_ACCOUNT_ID:?}" "${AWS_REGION:?}" "${CLUSTER_NAME:?}" "${BEDROCK_MODEL_ID:?}"
export AWS_ACCOUNT_ID AWS_REGION CLUSTER_NAME
ARC_VERSION="${ARC_VERSION:-0.13.0}"
ECR="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE="${ECR}/arc-intel-mcp:0.1.0"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

echo "==> EKS cluster (audit logging, Pod Identity, IRSA, NetworkPolicy, tainted runner nodes)"
envsubst < deploy/eks/cluster.yaml > "$WORK/cluster.yaml"
eksctl get cluster --name "$CLUSTER_NAME" --region "$AWS_REGION" >/dev/null 2>&1 \
  || eksctl create cluster -f "$WORK/cluster.yaml"
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$AWS_REGION"

echo "==> arc-intel image to ECR"
aws ecr describe-repositories --repository-names arc-intel-mcp >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name arc-intel-mcp --image-scanning-configuration scanOnPush=true \
       --image-tag-mutability IMMUTABLE >/dev/null
aws ecr get-login-password | docker login --username AWS --password-stdin "$ECR"
docker build --platform linux/amd64 -t "$IMAGE" mcp-server && docker push "$IMAGE"

echo "==> ARC controller on system nodes"
helm upgrade --install arc oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set-controller \
  -n arc-systems --create-namespace --version "$ARC_VERSION" --set nodeSelector.workload=system --wait

echo "==> External Secrets Operator + GitHub App secret from Secrets Manager"
helm repo add external-secrets https://charts.external-secrets.io >/dev/null && helm repo update >/dev/null
helm upgrade --install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace \
  --set serviceAccount.name=external-secrets --wait
kubectl create ns arc-runners --dry-run=client -o yaml | kubectl apply -f -
sed "s/us-west-2/${AWS_REGION}/" deploy/eks/runner-baseline/github-app-secret.yaml | kubectl apply -f -
kubectl apply -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: runners-egress, namespace: arc-runners}
spec:
  podSelector: {}
  policyTypes: [Egress]
  egress:
    - {}   # tighten to GitHub, ECR and your registries
EOF

echo "==> Runner scale sets on the tainted arc-runners node group"
for name in linux-standard linux-large; do
  helm upgrade --install "$name" oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set \
    -n arc-runners --version "$ARC_VERSION" \
    -f demo/values/linux-standard.yaml -f demo/values/eks-runner-placement.yaml \
    $([ "$name" = linux-large ] && echo "--set maxRunners=30")
done

echo "==> Monitoring (in-cluster alerts, remote-write to AMP)"
if [ -n "${AMP_WORKSPACE_ID:-}" ]; then
  sed -e "s/us-west-2/${AWS_REGION}/g" -e "s/ws-REPLACE/${AMP_WORKSPACE_ID}/" -e "s/arc-prod/${CLUSTER_NAME}/" \
    deploy/eks/monitoring/kube-prometheus-stack-amp.yaml > "$WORK/kps.yaml"
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null && helm repo update >/dev/null
  helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
    -n monitoring --create-namespace -f "$WORK/kps.yaml" --wait
fi

echo "==> Kagent"
helm upgrade --install kagent-crds oci://ghcr.io/kagent-dev/kagent/helm/kagent-crds -n kagent --create-namespace
helm upgrade --install kagent oci://ghcr.io/kagent-dev/kagent/helm/kagent -n kagent --wait   # see Kagent docs for provider defaults

echo "==> arc-intel (EKS overlay)"
cp -r deploy "$WORK/deploy"
( cd "$WORK/deploy/eks/mcp-server"
  kustomize edit set image "ghcr.io/my-org/arc-intel-mcp=${IMAGE}" )
sed -i -e "s#/aws/eks/arc-prod/cluster#/aws/eks/${CLUSTER_NAME}/cluster#" -e "s/us-west-2/${AWS_REGION}/" \
  "$WORK/deploy/eks/mcp-server/kustomization.yaml"
kubectl apply -k "$WORK/deploy/eks/mcp-server"
kubectl -n arc-intel rollout status deploy/arc-intel-mcp --timeout=180s
if [ -n "${AMP_WORKSPACE_ID:-}" ]; then kubectl apply -f deploy/monitoring/monitoring.yaml; fi

echo "==> Agent on Bedrock (IRSA)"
sed -i -e "s#REPLACE_WITH_CLAUDE_INFERENCE_PROFILE_ID#${BEDROCK_MODEL_ID}#" -e "s/us-west-2/${AWS_REGION}/" \
  "$WORK/deploy/eks/kagent/model-config-bedrock.yaml"
kubectl apply -k "$WORK/deploy/eks/kagent"
kubectl apply -k deploy/digest
kubectl -n kagent wait --for=condition=Ready agent/arc-compliance-agent --timeout=300s || true

cat <<EOF

Ready on EKS cluster ${CLUSTER_NAME}.
  demo/scripts/simulate-drift.sh
  kagent invoke -n kagent --agent arc-compliance-agent --task "Who changed arc-runners/linux-large, and what should we do?"
The answer now names the IAM role and session behind each manual change (EKS audit log).
EOF

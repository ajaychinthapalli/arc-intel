#!/usr/bin/env bash
# Add arc-intel and the arc-compliance-agent to an EKS cluster that ALREADY runs ARC and Kagent.
# It does not create a cluster, change node groups or touch your ARC/Kagent installs.
#
# What it creates:
#   AWS: ECR repo arc-intel-mcp, IAM role <cluster>-arc-intel-audit, one or two Pod Identity associations
#   Kubernetes: namespace arc-intel (MCP server), RemoteMCPServer + Agent in kagent
#
# Needs: aws, kubectl, docker (buildx), jq, python3; kubectl context pointing at the cluster.
# Optional env: CLUSTER_NAME, AWS_REGION, MODEL_CONFIG (default bedrock-native),
#               REFERENCE_AGENT (an agent whose Bedrock access already works; default arc-upgrade-readiness),
#               AGENT_SERVICE_ACCOUNT (skip detection), IMAGE_TAG (default 0.1.0)
set -euo pipefail
cd "$(dirname "$0")/../.."

CLUSTER_NAME="${CLUSTER_NAME:-ac-ws-dev-use2}"
AWS_REGION="${AWS_REGION:-us-east-2}"
MODEL_CONFIG="${MODEL_CONFIG:-bedrock-native}"
REFERENCE_AGENT="${REFERENCE_AGENT:-arc-upgrade-readiness}"
IMAGE_TAG="${IMAGE_TAG:-0.1.0}"
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE_REPO="${ECR}/arc-intel-mcp"
ROLE_NAME="${CLUSTER_NAME}-arc-intel-audit"
LOG_GROUP="/aws/eks/${CLUSTER_NAME}/cluster"
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

say() { printf '\n==> %s\n' "$*"; }
pia_role() {  # role ARN of the Pod Identity association for namespace/serviceaccount, if any
  local id
  id=$(aws eks list-pod-identity-associations --cluster-name "$CLUSTER_NAME" --namespace "$1" \
        --service-account "$2" --query 'associations[0].associationId' --output text 2>/dev/null || true)
  [ -n "$id" ] && [ "$id" != "None" ] || return 1
  aws eks describe-pod-identity-association --cluster-name "$CLUSTER_NAME" --association-id "$id" \
    --query 'association.roleArn' --output text
}

say "Preflight: cluster ${CLUSTER_NAME} in ${AWS_REGION}, account ${AWS_ACCOUNT_ID}"
kubectl get crd autoscalingrunnersets.actions.github.com agents.kagent.dev >/dev/null
kubectl -n kagent get modelconfig "$MODEL_CONFIG" >/dev/null
aws eks describe-cluster --name "$CLUSTER_NAME" \
  --query "cluster.logging.clusterLogging[?enabled].types[]" --output text | grep -qw audit \
  || { echo "EKS audit logging is off; enable it so arc-intel can name who changed what."; exit 1; }
GITHUB_CONFIG_URL="$(kubectl get autoscalingrunnersets -A -o jsonpath='{.items[0].spec.githubConfigUrl}')"
echo "ARC scale sets: $(kubectl get autoscalingrunnersets -A --no-headers | wc -l)  GitHub: ${GITHUB_CONFIG_URL}"

say "1/5 Image to ECR (${IMAGE_REPO}:${IMAGE_TAG})"
aws ecr describe-repositories --repository-names arc-intel-mcp >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name arc-intel-mcp \
       --image-scanning-configuration scanOnPush=true --image-tag-mutability IMMUTABLE >/dev/null
if aws ecr describe-images --repository-name arc-intel-mcp --image-ids imageTag="$IMAGE_TAG" >/dev/null 2>&1; then
  echo "tag ${IMAGE_TAG} already in ECR, skipping build"
else
  aws ecr get-login-password | docker login --username AWS --password-stdin "$ECR"
  docker buildx build --platform linux/amd64 -t "${IMAGE_REPO}:${IMAGE_TAG}" --push mcp-server
fi

say "2/5 IAM role + Pod Identity for arc-intel (read the EKS audit log only)"
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE_NAME" --description "arc-intel: read EKS audit log for drift attribution" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"pods.eks.amazonaws.com"},"Action":["sts:AssumeRole","sts:TagSession"]}]}' >/dev/null
fi
aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name eks-audit-log-read --policy-document "$(jq -n \
  --arg lg "arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:${LOG_GROUP}:*" \
  '{Version:"2012-10-17",Statement:[{Effect:"Allow",Action:["logs:StartQuery"],Resource:$lg},
    {Effect:"Allow",Action:["logs:GetQueryResults","logs:StopQuery"],Resource:"*"}]}')"
pia_role arc-intel arc-intel >/dev/null || aws eks create-pod-identity-association --cluster-name "$CLUSTER_NAME" \
  --namespace arc-intel --service-account arc-intel \
  --role-arn "arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ROLE_NAME}" >/dev/null
echo "arc-intel -> ${ROLE_NAME}"

say "3/5 Bedrock access for the agent (copy what ${REFERENCE_AGENT} already uses)"
if [ -z "${AGENT_SERVICE_ACCOUNT:-}" ]; then
  REF_SA="$(kubectl -n kagent get deploy "$REFERENCE_AGENT" -o jsonpath='{.spec.template.spec.serviceAccountName}' 2>/dev/null || true)"
  [ -n "$REF_SA" ] || { echo "Can't read ${REFERENCE_AGENT}'s ServiceAccount; set AGENT_SERVICE_ACCOUNT and re-run."; exit 1; }
  if REF_ROLE="$(pia_role kagent "$REF_SA")"; then
    # Pod Identity roles trust the EKS service, so the same role can serve a second ServiceAccount.
    AGENT_SERVICE_ACCOUNT=arc-compliance-agent
    kubectl -n kagent create serviceaccount "$AGENT_SERVICE_ACCOUNT" --dry-run=client -o yaml | kubectl apply -f -
    pia_role kagent "$AGENT_SERVICE_ACCOUNT" >/dev/null || aws eks create-pod-identity-association \
      --cluster-name "$CLUSTER_NAME" --namespace kagent --service-account "$AGENT_SERVICE_ACCOUNT" --role-arn "$REF_ROLE" >/dev/null
    echo "Pod Identity: ${AGENT_SERVICE_ACCOUNT} -> ${REF_ROLE}"
  else
    # IRSA trust is bound to one ServiceAccount, and static keys live in the ModelConfig:
    # in both cases the safe choice is to run as the same ServiceAccount.
    AGENT_SERVICE_ACCOUNT="$REF_SA"
    echo "Reusing ServiceAccount ${REF_SA} ($(kubectl -n kagent get sa "$REF_SA" \
      -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}' | sed 's/^$/no IRSA annotation/'))"
  fi
fi

say "4/5 Deploy arc-intel MCP server"
cp -r deploy "$WORK/deploy"
sed -i -e "s#IMAGE_REPO#${IMAGE_REPO}#" -e "s#IMAGE_TAG#${IMAGE_TAG}#" \
       -e "s#AWS_REGION_VALUE#${AWS_REGION}#" -e "s#CLUSTER_NAME_VALUE#${CLUSTER_NAME}#" \
       "$WORK/deploy/existing-eks/mcp-server/kustomization.yaml"
sed -i "s#GITHUB_CONFIG_URL#${GITHUB_CONFIG_URL}#" "$WORK/deploy/existing-eks/mcp-server/policy.yaml"
kubectl apply -k "$WORK/deploy/existing-eks/mcp-server"
kubectl -n arc-intel rollout status deploy/arc-intel-mcp --timeout=180s

say "5/5 Register tools and create the agent in Kagent"
sed -i -e "s#MODEL_CONFIG_NAME#${MODEL_CONFIG}#" -e "s#AGENT_SERVICE_ACCOUNT#${AGENT_SERVICE_ACCOUNT}#" \
  "$WORK/deploy/existing-eks/kagent/kustomization.yaml"
kubectl apply -k "$WORK/deploy/existing-eks/kagent"
kubectl -n kagent wait --for=condition=Ready agent/arc-compliance-agent --timeout=300s || true

say "Smoke test"
kubectl -n arc-intel port-forward svc/arc-intel-mcp 18080:8080 >/dev/null 2>&1 &
PF=$!; trap 'kill $PF 2>/dev/null; rm -rf "$WORK"' EXIT; sleep 3
call() {
  curl -s localhost:18080/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H 'MCP-Protocol-Version: 2025-06-18' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" \
  | jq "$3"
}
call arc_fleet_report '{}' '.result.structuredContent | {fleet_grade, fleet_score, postures, controllers: [.controllers[] | {name, version}]}'
call arc_detect_drift '{"namespace":"arc-runners","name":"arc-runner-set"}' \
  '.result.structuredContent | {baseline_source, drifted, summary}'

cat <<EOF

Done. Ask the agent:
  kagent invoke -n kagent --agent arc-compliance-agent --task "How compliant are our ARC runners? Worst issues first."
Drift drill (dev cluster): NS=arc-runners NAME=arc-runner-set demo/scripts/simulate-drift.sh
Undo the drill:            helm upgrade arc-runner-set oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set \\
                             -n arc-runners --version 0.14.2 --reuse-values
EOF

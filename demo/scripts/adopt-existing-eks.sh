#!/usr/bin/env bash
# Put an EXISTING EKS cluster (ARC + Kagent already installed with Helm) under Argo CD, following the
# architecture diagram: Argo CD is the source of truth, arc-intel reads its target state.
#
#   demo/scripts/adopt-existing-eks.sh <phase>
#
# Phases, in order (each is safe to re-run):
#   discover   read-only: what is installed, what will be adopted
#   aws        ECR image, IAM roles + Pod Identity (arc-intel, External Secrets, agent),
#              copy the GitHub config Secret into Secrets Manager
#   generate   write clusters/$CLUSTER_NAME/ from live Helm values  -> review, commit, push
#   argocd     install Argo CD, read-only arc-intel account, token -> Secrets Manager
#   bootstrap  apply the root app; ARC apps wait for your reviewed manual sync
#   verify     Argo CD app status + arc-intel smoke test (source of truth = Argo CD)
#
# Env: CLUSTER_NAME (ac-ws-dev-use2), AWS_REGION (us-east-2), GITOPS_REPO (required from `generate`),
#      GITOPS_REVISION (main), GITOPS_USERNAME/GITOPS_TOKEN (private repo, read-only),
#      MODEL_CONFIG (bedrock-native), REFERENCE_AGENT (arc-upgrade-readiness), IMAGE_TAG (0.1.0),
#      ESO_VERSION / KPS_VERSION / ARGOCD_CHART_VERSION (default: latest at generate time, then pinned)
set -euo pipefail
cd "$(dirname "$0")/../.."

PHASE="${1:-}"
CLUSTER_NAME="${CLUSTER_NAME:-ac-ws-dev-use2}"
AWS_REGION="${AWS_REGION:-us-east-2}"
GITOPS_REVISION="${GITOPS_REVISION:-main}"
MODEL_CONFIG="${MODEL_CONFIG:-bedrock-native}"
REFERENCE_AGENT="${REFERENCE_AGENT:-arc-upgrade-readiness}"
IMAGE_TAG="${IMAGE_TAG:-0.1.0}"
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION"

say()  { printf '\n==> %s\n' "$*"; }
need() { for c in "$@"; do command -v "$c" >/dev/null || { echo "missing tool: $c"; exit 1; }; done; }
account() { aws sts get-caller-identity --query Account --output text; }
pia_role() {  # role ARN of the Pod Identity association for <namespace> <serviceaccount>
  local id
  id=$(aws eks list-pod-identity-associations --cluster-name "$CLUSTER_NAME" --namespace "$1" \
        --service-account "$2" --query 'associations[0].associationId' --output text 2>/dev/null || true)
  [ -n "$id" ] && [ "$id" != "None" ] || return 1
  aws eks describe-pod-identity-association --cluster-name "$CLUSTER_NAME" --association-id "$id" \
    --query 'association.roleArn' --output text
}
ensure_role() {  # <role-name> <policy-name> <policy-json>  (trusts EKS Pod Identity)
  aws iam get-role --role-name "$1" >/dev/null 2>&1 || aws iam create-role --role-name "$1" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"pods.eks.amazonaws.com"},"Action":["sts:AssumeRole","sts:TagSession"]}]}' >/dev/null
  aws iam put-role-policy --role-name "$1" --policy-name "$2" --policy-document "$3"
}
ensure_pia() {  # <namespace> <serviceaccount> <role-arn>
  pia_role "$1" "$2" >/dev/null || aws eks create-pod-identity-association --cluster-name "$CLUSTER_NAME" \
    --namespace "$1" --service-account "$2" --role-arn "$3" >/dev/null
  echo "Pod Identity: $1/$2 -> $3"
}
releases_json() { helm list -A -o json; }
controller_release() { releases_json | jq -c '[.[] | select(.chart|startswith("gha-runner-scale-set-controller-"))][0]'; }
scale_set_releases() { releases_json | jq -c '[.[] | select(.chart|test("^gha-runner-scale-set-[0-9]"))]'; }
github_secret_name() { kubectl get autoscalingrunnersets -A -o jsonpath='{.items[0].spec.githubConfigSecret}'; }
runner_ns() { kubectl get autoscalingrunnersets -A -o jsonpath='{.items[0].metadata.namespace}'; }
agent_sa() {  # prints "<sa> <create:true|false>"
  if [ -n "${AGENT_SERVICE_ACCOUNT:-}" ]; then echo "$AGENT_SERVICE_ACCOUNT false"; return; fi
  local ref; ref="$(kubectl -n kagent get deploy "$REFERENCE_AGENT" -o jsonpath='{.spec.template.spec.serviceAccountName}')"
  if pia_role kagent "$ref" >/dev/null; then echo "arc-compliance-agent true"; else echo "$ref false"; fi
}
latest_chart() {  # <repo-name> <repo-url> <chart>
  helm repo add "$1" "$2" >/dev/null 2>&1 || true
  helm repo update "$1" >/dev/null
  helm search repo "$1/$3" -o json | jq -r '.[0].version'
}

# ------------------------------------------------------------------------------------------------
discover() {
  need kubectl helm jq aws
  say "Cluster ${CLUSTER_NAME} (${AWS_REGION})"
  aws eks describe-cluster --name "$CLUSTER_NAME" \
    --query 'cluster.{version:version,audit:logging.clusterLogging[?enabled].types[]}' --output json
  say "ARC Helm releases (will be adopted by Argo CD, not reinstalled)"
  controller_release | jq '{release:.name, namespace, chart}'
  scale_set_releases | jq '.[] | {release:.name, namespace, chart}'
  say "Scale sets"
  kubectl get autoscalingrunnersets -A \
    -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,GITHUB:.spec.githubConfigUrl,SECRET:.spec.githubConfigSecret,MIN:.spec.minRunners,MAX:.spec.maxRunners'
  say "GitHub config Secret key names (values not shown)"
  kubectl -n "$(runner_ns)" get secret "$(github_secret_name)" -o json | jq -r '.data | keys[]'
  say "Agent Bedrock access will be copied from ${REFERENCE_AGENT}: ServiceAccount $(agent_sa)"
  say "Argo CD installed?"; kubectl get ns argocd >/dev/null 2>&1 && echo yes || echo "no (phase argocd installs it)"
  say "VPC CNI NetworkPolicy enforcement"
  aws eks describe-addon --cluster-name "$CLUSTER_NAME" --addon-name vpc-cni \
    --query addon.configurationValues --output text | grep -q '"enableNetworkPolicy": *"true"' \
    && echo enabled || echo "not enabled: NetworkPolicies exist but aren't enforced (optional add-on setting)"
}

# ------------------------------------------------------------------------------------------------
aws_phase() {
  need aws kubectl jq docker
  local acct ecr role
  acct="$(account)"; ecr="${acct}.dkr.ecr.${AWS_REGION}.amazonaws.com"

  say "arc-intel image -> ECR"
  aws ecr describe-repositories --repository-names arc-intel-mcp >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name arc-intel-mcp \
         --image-scanning-configuration scanOnPush=true --image-tag-mutability IMMUTABLE >/dev/null
  if aws ecr describe-images --repository-name arc-intel-mcp --image-ids imageTag="$IMAGE_TAG" >/dev/null 2>&1; then
    echo "tag ${IMAGE_TAG} already pushed"
  else
    aws ecr get-login-password | docker login --username AWS --password-stdin "$ecr"
    docker buildx build --platform linux/amd64 -t "${ecr}/arc-intel-mcp:${IMAGE_TAG}" --push mcp-server
  fi

  say "GitHub config Secret -> Secrets Manager arc/github-app"
  if aws secretsmanager describe-secret --secret-id arc/github-app >/dev/null 2>&1; then
    echo "arc/github-app already exists (not overwritten)"
  else
    kubectl -n "$(runner_ns)" get secret "$(github_secret_name)" -o json \
      | jq '.data | map_values(@base64d)' \
      | aws secretsmanager create-secret --name arc/github-app --description "ARC GitHub config for ${CLUSTER_NAME}" \
          --secret-string file:///dev/stdin >/dev/null
    echo "created with keys: $(kubectl -n "$(runner_ns)" get secret "$(github_secret_name)" -o json | jq -r '.data|keys|join(", ")')"
  fi

  say "IAM + Pod Identity"
  role="${CLUSTER_NAME}-arc-intel-audit"
  ensure_role "$role" eks-audit-log-read "$(jq -n \
    --arg lg "arn:aws:logs:${AWS_REGION}:${acct}:log-group:/aws/eks/${CLUSTER_NAME}/cluster:*" \
    '{Version:"2012-10-17",Statement:[{Effect:"Allow",Action:["logs:StartQuery"],Resource:$lg},
      {Effect:"Allow",Action:["logs:GetQueryResults","logs:StopQuery"],Resource:"*"}]}')"
  ensure_pia arc-intel arc-intel "arn:aws:iam::${acct}:role/${role}"

  role="${CLUSTER_NAME}-external-secrets"
  ensure_role "$role" read-arc-secrets "$(jq -n \
    --arg s "arn:aws:secretsmanager:${AWS_REGION}:${acct}:secret:arc/*" \
    '{Version:"2012-10-17",Statement:[{Effect:"Allow",Action:["secretsmanager:GetSecretValue","secretsmanager:DescribeSecret"],Resource:$s}]}')"
  ensure_pia external-secrets external-secrets "arn:aws:iam::${acct}:role/${role}"

  read -r sa create <<<"$(agent_sa)"
  if [ "$create" = true ]; then
    local ref; ref="$(kubectl -n kagent get deploy "$REFERENCE_AGENT" -o jsonpath='{.spec.template.spec.serviceAccountName}')"
    ensure_pia kagent "$sa" "$(pia_role kagent "$ref")"
  else
    echo "Agent reuses ServiceAccount ${sa} (same Bedrock access as ${REFERENCE_AGENT})"
  fi
}

# ------------------------------------------------------------------------------------------------
generate() {
  need kubectl helm jq aws python3
  : "${GITOPS_REPO:?set GITOPS_REPO to your fork, e.g. https://github.com/<org>/arc-intel.git}"
  local ctrl sets acct ctx sa create eso kps arc
  ctrl="$(controller_release)"; sets="$(scale_set_releases)"; acct="$(account)"
  arc="$(jq -r '.chart | sub("^gha-runner-scale-set-controller-";"")' <<<"$ctrl")"
  for v in $(jq -r '.[].chart | sub("^gha-runner-scale-set-";"")' <<<"$sets"); do
    [ "$v" = "$arc" ] || { echo "Scale set chart $v != controller $arc: align versions before adopting."; exit 1; }
  done
  [ "$(jq -r '[.[].namespace]|unique|length' <<<"$sets")" = 1 ] \
    || { echo "Scale sets span several namespaces; adopt one namespace at a time."; exit 1; }
  eso="${ESO_VERSION:-$(latest_chart external-secrets https://charts.external-secrets.io external-secrets)}"
  kps="${KPS_VERSION:-$(latest_chart prometheus-community https://prometheus-community.github.io/helm-charts kube-prometheus-stack)}"
  read -r sa create <<<"$(agent_sa)"

  ctx="$(mktemp)"
  {
    jq -n \
      --arg cluster "$CLUSTER_NAME" --arg region "$AWS_REGION" --arg repo "$GITOPS_REPO" --arg rev "$GITOPS_REVISION" \
      --arg image "${acct}.dkr.ecr.${AWS_REGION}.amazonaws.com/arc-intel-mcp" --arg tag "$IMAGE_TAG" \
      --arg gh "$(kubectl get autoscalingrunnersets -A -o jsonpath='{.items[0].spec.githubConfigUrl}')" \
      --arg ghs "$(github_secret_name)" --arg mc "$MODEL_CONFIG" --arg sa "$sa" --argjson create "$create" \
      --arg arc "$arc" --arg eso "$eso" --arg kps "$kps" \
      --arg crel "$(jq -r .name <<<"$ctrl")" --arg cns "$(jq -r .namespace <<<"$ctrl")" \
      --argjson cvals "$(helm get values "$(jq -r .name <<<"$ctrl")" -n "$(jq -r .namespace <<<"$ctrl")" -o json)" \
      --arg rns "$(jq -r '.[0].namespace' <<<"$sets")" \
      --argjson sets "$(jq -c '[.[] | {release:.name, namespace}]' <<<"$sets" | jq -c '.' )" \
      '{cluster:$cluster, region:$region, repo_url:$repo, repo_revision:$rev, image_repo:$image, image_tag:$tag,
        github_config_url:$gh, github_secret_name:$ghs, model_config:$mc, agent_sa:$sa, agent_sa_create:$create,
        versions:{arc:$arc, eso:$eso, kps:$kps},
        controller:{release:$crel, namespace:$cns, values:$cvals}, runner_namespace:$rns, scale_sets:$sets}'
  } > "$ctx"
  # attach each scale set's values
  for rel in $(jq -r '.scale_sets[].release' "$ctx"); do
    ns="$(jq -r --arg r "$rel" '.scale_sets[]|select(.release==$r).namespace' "$ctx")"
    jq --arg r "$rel" --argjson v "$(helm get values "$rel" -n "$ns" -o json)" \
      '(.scale_sets[]|select(.release==$r)).values = $v' "$ctx" > "$ctx.tmp" && mv "$ctx.tmp" "$ctx"
  done
  python3 clusters/render.py "$ctx" $([ "${FORCE:-}" = 1 ] && echo --force)
  rm -f "$ctx"
  cat <<EOF

Review clusters/${CLUSTER_NAME}/ (start with ADOPTION-NOTES.md), then:
  git add clusters/${CLUSTER_NAME} && git commit -m "Adopt ${CLUSTER_NAME} into Argo CD" && git push
Next: demo/scripts/adopt-existing-eks.sh argocd
EOF
}

# ------------------------------------------------------------------------------------------------
argocd_phase() {
  need kubectl helm jq aws curl
  local ver values pw tok
  ver="${ARGOCD_CHART_VERSION:-$(latest_chart argo https://argoproj.github.io/argo-helm argo-cd)}"
  say "Argo CD (chart ${ver})"
  values="$(mktemp)"
  cat > "$values" <<'EOF'
configs:
  cm:
    application.resourceTrackingMethod: annotation
    accounts.arc-intel: apiKey
    # App-of-apps sync waves wait for child Applications to be healthy.
    resource.customizations.health.argoproj.io_Application: |
      hs = {status = "Progressing", message = ""}
      if obj.status ~= nil and obj.status.health ~= nil then
        hs.status = obj.status.health.status
        if obj.status.health.message ~= nil then hs.message = obj.status.health.message end
      end
      return hs
  rbac:
    policy.csv: |
      p, role:arc-intel-readonly, applications, get, */*, allow
      g, arc-intel, role:arc-intel-readonly
EOF
  helm upgrade --install argocd argo/argo-cd -n argocd --create-namespace --version "$ver" -f "$values" --wait
  rm -f "$values"

  if [ -n "${GITOPS_REPO:-}" ] && [ -n "${GITOPS_TOKEN:-}" ]; then
    say "Repository credentials (read-only) for ${GITOPS_REPO}"
    kubectl -n argocd create secret generic repo-gitops --dry-run=client -o yaml \
      --from-literal=type=git --from-literal=url="$GITOPS_REPO" \
      --from-literal=username="${GITOPS_USERNAME:-git}" --from-literal=password="$GITOPS_TOKEN" \
      | kubectl label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | kubectl apply -f -
  fi

  say "arc-intel read-only token -> Secrets Manager arc/argocd-token"
  if aws secretsmanager describe-secret --secret-id arc/argocd-token >/dev/null 2>&1; then
    echo "arc/argocd-token already exists (delete it to rotate)"
    return
  fi
  kubectl -n argocd port-forward svc/argocd-server 18443:443 >/dev/null 2>&1 & local pf=$!
  sleep 4
  pw="$(kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)"
  tok="$(curl -sk https://localhost:18443/api/v1/session -d "$(jq -n --arg p "$pw" '{username:"admin",password:$p}')" | jq -r .token)"
  curl -sk -X POST -H "Authorization: Bearer $tok" https://localhost:18443/api/v1/account/arc-intel/token -d '{}' \
    | jq '{token:.token}' | aws secretsmanager create-secret --name arc/argocd-token \
        --description "Argo CD read-only token for arc-intel (${CLUSTER_NAME})" --secret-string file:///dev/stdin >/dev/null
  kill "$pf" 2>/dev/null || true
  echo "stored. Change the Argo CD admin password and delete argocd-initial-admin-secret when you're done."
}

# ------------------------------------------------------------------------------------------------
bootstrap() {
  need kubectl jq
  local root="clusters/${CLUSTER_NAME}/root-app.yaml"
  [ -f "$root" ] || { echo "$root missing: run generate, commit and push first."; exit 1; }
  kubectl apply -f "$root"
  say "Waiting for the ARC applications to appear (they do not sync on their own)"
  for _ in $(seq 60); do
    kubectl -n argocd get application arc-controller >/dev/null 2>&1 && break; sleep 5
  done
  sleep 20
  kubectl -n argocd get applications -o custom-columns='APP:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'
  cat <<EOF

Review what Argo CD would change on the adopted ARC apps before the first sync:
  argocd app diff arc-controller
  argocd app diff arc-runners-<release>
Expected: tracking annotations only (plus nothing else). Then:
  argocd app sync arc-controller && argocd app sync arc-runners-<release>
Finally: demo/scripts/adopt-existing-eks.sh verify
EOF
}

# ------------------------------------------------------------------------------------------------
verify() {
  need kubectl jq curl
  say "Argo CD applications"
  kubectl -n argocd get applications -o custom-columns='APP:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'
  say "arc-intel"
  kubectl -n arc-intel rollout status deploy/arc-intel-mcp --timeout=120s
  kubectl -n arc-intel port-forward svc/arc-intel-mcp 18080:8080 >/dev/null 2>&1 & local pf=$!
  sleep 3
  call() {
    curl -s localhost:18080/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
      -H 'MCP-Protocol-Version: 2025-06-18' \
      -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" | jq "$3"
  }
  call arc_fleet_report '{}' '.result.structuredContent | {fleet_grade, fleet_score, postures}'
  for name in $(kubectl get autoscalingrunnersets -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {end}'); do
    call arc_detect_drift "{\"namespace\":\"${name%/*}\",\"name\":\"${name#*/}\"}" \
      '.result.structuredContent | {name, baseline_source, drifted, identity_source, gitops: .gitops | {application, sync_status, self_heal}}'
  done
  kill "$pf" 2>/dev/null || true
  echo
  echo "Expect baseline_source \"argocd:arc-runners-<release>\" and identity_source \"eks_audit_log\"."
  echo "Then: kagent invoke -n kagent --agent arc-compliance-agent --task \"How compliant are our ARC runners?\""
}

case "$PHASE" in
  discover) discover ;; aws) aws_phase ;; generate) generate ;;
  argocd) argocd_phase ;; bootstrap) bootstrap ;; verify) verify ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac

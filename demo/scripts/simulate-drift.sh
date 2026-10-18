#!/usr/bin/env bash
# Simulate the two classic ARC drift incidents on the Helm-managed linux-large scale set:
#   1. someone bumps maxRunners to 200 "to clear the queue"      (field manager: kubectl-patch)
#   2. someone swaps the runner image to :latest to "fix a bug"  (field manager: kubectl-patch)
# The agent should report posture drift_introduced_violation, attribute both changes, and propose a revert.
set -euo pipefail
NS="${NS:-arc-runners}"
NAME="${NAME:-linux-large}"

kubectl -n "$NS" patch autoscalingrunnerset "$NAME" --type=merge -p '{"spec":{"maxRunners":200}}'
IDX=$(kubectl -n "$NS" get autoscalingrunnerset "$NAME" -o json \
  | python3 -c 'import json,sys; cs=json.load(sys.stdin)["spec"]["template"]["spec"]["containers"]; print([c["name"] for c in cs].index("runner"))')
kubectl -n "$NS" patch autoscalingrunnerset "$NAME" --type=json \
  -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/${IDX}/image\",\"value\":\"ghcr.io/actions/actions-runner:latest\"}]"

echo
echo "Drift injected. Ask the agent:"
echo "  kagent invoke -n kagent --agent arc-compliance-agent --task \"Has anyone changed $NS/$NAME by hand? What should we do?\""
echo
echo "Revert with:  helm upgrade $NAME oci://ghcr.io/actions/actions-runner-controller-charts/gha-runner-scale-set -n $NS --reuse-values"

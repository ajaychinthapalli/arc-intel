# Adopting an existing EKS cluster into Argo CD

For a cluster that already runs ARC and Kagent with Helm (for example `ac-ws-dev-use2`). The target is
the architecture diagram: Argo CD is the source of truth for the runner scale sets, arc-intel reads
Argo CD's target state, and every change to a runner pool goes through Git.

Nothing is reinstalled. Argo CD adopts the existing Helm releases with the same chart version,
release names and values, so pods keep running.

```bash
export CLUSTER_NAME=ac-ws-dev-use2 AWS_REGION=us-east-2
export GITOPS_REPO=https://github.com/<org>/arc-intel.git      # your fork of this repo
S=demo/scripts/adopt-existing-eks.sh
```

| Phase | Command | Changes | Diagram |
|---|---|---|---|
| 1 Discover | `$S discover` | Nothing (read-only) | — |
| 2 AWS | `$S aws` | ECR image; IAM roles + Pod Identity for arc-intel, External Secrets, agent; GitHub config Secret copied to Secrets Manager `arc/github-app` | AWS services strip |
| 3 Generate | `$S generate`, then commit + push | Writes `clusters/$CLUSTER_NAME/` from live Helm values | Layer 4: Argo CD (Git) |
| 4 Argo CD | `$S argocd` | Installs Argo CD; read-only `arc-intel` account; token to Secrets Manager `arc/argocd-token` | Layer 4: Argo CD |
| 5 Bootstrap | `$S bootstrap` | Root app; External Secrets, monitoring, arc-intel and agent sync automatically; ARC apps wait | Layers 1–3 |
| 6 Adopt ARC | `argocd app diff` then `argocd app sync` | Argo CD takes ownership of the controller and scale sets | Layer 4: ARC resources |
| 7 Verify | `$S verify` | Nothing | Flows 1–6, A–C |

## Phase 6: adopting ARC safely

The ARC applications are created **without automated sync** and **without the cascade-delete finalizer**:

1. `argocd app diff arc-controller` and `argocd app diff arc-runners-<release>`. Expect only Argo CD's
   tracking annotation, plus the chart's `actions.github.com/values-hash` annotation if inline credentials
   were moved out of the values (see `ADOPTION-NOTES.md`). Anything else means the exported values don't
   match what is running: fix the values file in Git rather than syncing.
2. `argocd app sync arc-controller`, then each `arc-runners-<release>`.
3. When both stay Synced and Healthy, open a PR adding to `apps/20-arc.yaml`
   (`arc-controller` and the ApplicationSet template):
   ```yaml
   syncPolicy:
     automated: {prune: false, selfHeal: true}
   ```
   From then on, a `kubectl edit` on a scale set is reverted by Argo CD and still reported by arc-intel
   (`self_healed`, `out_of_band_writes`) with the IAM identity from the EKS audit log.

## After adoption

- **Stop using Helm for ARC.** The old release records stay in the cluster. A `helm upgrade` would fight
  Argo CD, and `helm uninstall` would delete the runners. Once stable for a week, remove only the history
  (workloads stay):
  `kubectl -n arc-systems delete secret -l owner=helm,name=arc` and
  `kubectl -n arc-runners delete secret -l owner=helm,name=<release>`.
- **Credentials:** if the scale set used inline credentials (a PAT in Helm values), they now live in
  Secrets Manager `arc/github-app`. Rotate the token, and move to a GitHub App to clear rule ARC-001.
- **First findings are expected:** unpinned runner image, missing owner labels, or Docker-in-Docker will show
  up as `non_compliant`. Fix them by PR to `runner-scale-sets/<release>/values.yaml`. That is the workflow
  the diagram describes.
- **Kagent** stays Helm-managed here; only the agent, tools and digest are Argo CD apps. Adopt Kagent later
  the same way if you want everything in Git (its values carry provider keys, so move those to Secrets first).
- **argocd-server TLS:** arc-intel accepts Argo CD's self-signed certificate on this dev cluster
  (`ARGOCD_INSECURE=true` in `manifests/arc-intel`). For production, issue a certificate from your CA and drop it.

## Rollback

Delete the ARC Applications (`kubectl -n argocd delete application arc-controller arc-runners-<release>`):
without the finalizer, the controller and runners keep running and the Helm releases are still there.

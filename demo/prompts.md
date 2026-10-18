# Demo prompts for `arc-compliance-agent`

Run in the Kagent dashboard or with
`kagent invoke -n kagent --agent arc-compliance-agent --task "<prompt>"`.

## Fleet posture
- How compliant is our ARC fleet right now? Worst offenders first.
- Which scale sets would fail a SOC 2 review of CI runner isolation, and why?
- Are the ARC controller and all scale sets on the same version?

## Drift investigation
- Has anyone changed arc-runners/linux-large by hand? Who, what and when?
- Why is the large Linux runner pool scaling to 200 runners?
- Show every manual change across all runner scale sets in the last day.

## Remediation
- How do we fix arc-runners-legacy/legacy-build? Split it into what the owning team changes in Helm values versus what platform does.
- Give me the safest way to revert the drift on linux-large right now.

## Baselines (requires `ALLOW_BASELINE_WRITE=true`)
- The RUNNER_DEBUG flag on arc-runners-docker/docker-builds is approved under CHG-2291. Record the current state as its baseline.

## Correlation
- Ephemeral runners in arc-runners keep failing. Is it configuration or something else? Check events and listener logs.

## What a good answer looks like (linux-large after `simulate-drift.sh`)
> **Posture: drift_introduced_violation (score 80, B).** Two live changes broke policy; the Helm release is compliant.
>
> | sev | rule / path | what | origin | who / when |
> |---|---|---|---|---|
> | high | ARC-005 `containers[runner].image` | `:latest` replaced pinned 2.328.0@sha256 | drift | kubectl-patch, 2026-10-17 03:12Z |
> | high | ARC-003 `spec.maxRunners` | 200 vs ceiling 50 (Helm says 30) | drift | kubectl-edit, 2026-10-16 22:41Z |
>
> **Recommended actions:** 1) `helm upgrade linux-large … --reuse-values` to restore the release; 2) if the queue genuinely needs more capacity, raise maxRunners in Git within the ceiling; 3) find out who ran the patches and route future changes through the values repo.

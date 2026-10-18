"""Realistic ARC cluster snapshot used by tests and the offline demo.

Run `python tests/fixtures.py <out.yaml>` to regenerate demo/offline/cluster.yaml.

Scenarios
---------
arc-runners/linux-standard      healthy: compliant, live == Helm release
arc-runners/linux-large         drift_introduced_violation: someone `kubectl edit`ed maxRunners to 200
                                and `kubectl patch`ed the runner image to :latest
arc-runners-docker/docker-builds drifted: Argo-managed DinD set (allowed by override) with a debug env var
                                added by hand; baseline captured in the arc-intel ConfigMap
arc-runners/arm64-builds        drifted: Argo CD-managed (tracking-id), runner CPU limit raised by kubectl-edit,
                                desired state from the Argo CD API (offline: ArgoTargetState docs)
arc-runners-legacy/legacy-build non_compliant: hand-made, PAT auth, privileged DinD, hostPath, unbounded,
                                Default group, docker.io image, old chart version, no NetworkPolicy
"""

from __future__ import annotations

import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arc_intel.cluster import BASELINE_CONFIGMAP, encode_helm_release  # noqa: E402

VERSION = "0.13.0"
RUNNER = "ghcr.io/actions/actions-runner:2.328.0@sha256:5f0c1e7f1b6a4d6b9c7f3e2a1d0c9b8a7f6e5d4c3b2a19087f6e5d4c3b2a1908"


def _runner_container(image=RUNNER, extra_env=None):
    env = [{"name": "ACTIONS_RUNNER_CONTAINER_HOOKS", "value": "/home/runner/k8s/index.js"}] + (extra_env or [])
    return {
        "name": "runner",
        "image": image,
        "command": ["/home/runner/run.sh"],
        "env": env,
        "resources": {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"cpu": "2", "memory": "4Gi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "runAsNonRoot": True, "runAsUser": 1001},
    }


def _ars(ns, name, spec, labels=None, annotations=None, managed_fields=None):
    return {
        "apiVersion": "actions.github.com/v1alpha1",
        "kind": "AutoscalingRunnerSet",
        "metadata": {
            "name": name, "namespace": ns,
            "labels": labels or {}, "annotations": annotations or {},
            "managedFields": managed_fields or [],
            "resourceVersion": "48211",
        },
        "spec": spec,
        "status": {"currentRunners": 2, "pendingEphemeralRunners": 0, "runningEphemeralRunners": 2},
    }


def _helm_labels(release, owner="platform", cc="cc-1042"):
    return {
        "app.kubernetes.io/managed-by": "Helm", "app.kubernetes.io/instance": release,
        "app.kubernetes.io/version": VERSION, "app.kubernetes.io/part-of": "gha-rs",
        "owner": owner, "cost-center": cc,
    }


def _helm_secret(ns, release, docs):
    manifest = "\n---\n".join(json.dumps(d) for d in docs)  # JSON is valid YAML
    rel = {"name": release, "namespace": ns, "version": 3, "info": {"status": "deployed"}, "manifest": manifest}
    return {
        "apiVersion": "v1", "kind": "Secret", "type": "helm.sh/release.v1",
        "metadata": {"name": f"sh.helm.release.v1.{release}.v3", "namespace": ns,
                     "labels": {"owner": "helm", "name": release, "status": "deployed", "version": "3"}},
        "data": {"release": encode_helm_release(rel)},
    }


def _desired(ars):
    d = copy.deepcopy(ars)
    d["metadata"] = {k: d["metadata"][k] for k in ("name", "namespace", "labels")}
    d["metadata"]["annotations"] = {"meta.helm.sh/release-name": d["metadata"]["labels"].get("app.kubernetes.io/instance", "")}
    d.pop("status")
    return d


HELM_MF = {"manager": "helm", "operation": "Update", "time": "2026-10-01T09:00:00Z", "fieldsType": "FieldsV1",
           "fieldsV1": {"f:metadata": {"f:labels": {".": {}}}, "f:spec": {
               "f:githubConfigUrl": {}, "f:githubConfigSecret": {}, "f:maxRunners": {}, "f:minRunners": {},
               "f:runnerGroup": {}, "f:template": {"f:spec": {"f:containers": {}}}}}}


def build() -> list[dict]:
    objs: list[dict] = []

    # --- controller -----------------------------------------------------------------------------------
    objs.append({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": "arc-gha-rs-controller", "namespace": "arc-systems",
                     "labels": {"app.kubernetes.io/part-of": "gha-rs-controller", "app.kubernetes.io/version": VERSION}},
        "spec": {"template": {"spec": {"containers": [{"name": "manager", "image": f"ghcr.io/actions/gha-runner-scale-set-controller:{VERSION}"}]}}},
    })

    # --- arc-runners: healthy + drifted ------------------------------------------------------------------
    ns = "arc-runners"
    objs.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                 "metadata": {"name": "runners-egress", "namespace": ns}, "spec": {"podSelector": {}}})
    objs.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "arc-github-app", "namespace": ns},
                 "data": {"github_app_id": "MTIzNDU=", "github_app_installation_id": "Njc4OTA=", "github_app_private_key": "LS0tLS1CRUdJTi0tLS0t"}})

    base_spec = {
        "githubConfigUrl": "https://github.com/my-org", "githubConfigSecret": "arc-github-app",
        "runnerGroup": "platform", "minRunners": 1, "maxRunners": 20,
        "template": {"spec": {"serviceAccountName": "arc-runner", "containers": [_runner_container()]}},
    }
    std = _ars(ns, "linux-standard", copy.deepcopy(base_spec), _helm_labels("linux-standard"),
               {"meta.helm.sh/release-name": "linux-standard"}, [HELM_MF])
    objs += [std, _helm_secret(ns, "linux-standard", [_desired(std)])]

    large_spec = copy.deepcopy(base_spec)
    large_spec["maxRunners"] = 30
    large_desired = _ars(ns, "linux-large", large_spec, _helm_labels("linux-large"), {"meta.helm.sh/release-name": "linux-large"})
    large = copy.deepcopy(large_desired)
    large["spec"]["maxRunners"] = 200
    large["spec"]["template"]["spec"]["containers"][0]["image"] = "ghcr.io/actions/actions-runner:latest"
    large["metadata"]["managedFields"] = [
        HELM_MF,
        {"manager": "kubectl-edit", "operation": "Update", "time": "2026-10-16T22:41:07Z", "fieldsType": "FieldsV1",
         "fieldsV1": {"f:spec": {"f:maxRunners": {}}}},
        {"manager": "kubectl-patch", "operation": "Update", "time": "2026-10-17T03:12:55Z", "fieldsType": "FieldsV1",
         "fieldsV1": {"f:spec": {"f:template": {"f:spec": {"f:containers": {'k:{"name":"runner"}': {"f:image": {}}}}}}}},
    ]
    objs += [large, _helm_secret(ns, "linux-large", [
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "should-never-be-returned"}, "stringData": {"github_token": "ghp_x"}},
        _desired(large_desired),
    ])]
    objs += [
        {"apiVersion": "actions.github.com/v1alpha1", "kind": "EphemeralRunner",
         "metadata": {"name": f"linux-large-{i}", "namespace": ns, "labels": {"actions.github.com/scale-set-name": "linux-large"}},
         "status": {"phase": phase}}
        for i, phase in enumerate(["Running", "Running", "Failed"])
    ]

    # --- arc-runners-docker: Argo-managed DinD with manual env drift --------------------------------------
    ns = "arc-runners-docker"
    objs.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                 "metadata": {"name": "runners-egress", "namespace": ns}, "spec": {"podSelector": {}}})
    objs.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "arc-github-app", "namespace": ns},
                 "data": {"github_app_id": "MQ==", "github_app_installation_id": "Mg==", "github_app_private_key": "LQ=="}})
    dind_spec = copy.deepcopy(base_spec)
    dind_spec["maxRunners"] = 40
    dind_spec["template"]["spec"]["containers"].append({
        "name": "dind", "image": "ghcr.io/my-org/docker:27.3-dind",
        "securityContext": {"privileged": True},
        "resources": {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"cpu": "4", "memory": "8Gi"}},
    })
    dlabels = {"argocd.argoproj.io/instance": "ci-runners", "app.kubernetes.io/version": VERSION,
               "owner": "build-infra", "cost-center": "cc-2201"}
    docker_baseline = _ars(ns, "docker-builds", copy.deepcopy(dind_spec), dict(dlabels))
    docker_live = copy.deepcopy(docker_baseline)
    docker_live["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "RUNNER_DEBUG", "value": "1"})
    docker_live["metadata"]["managedFields"] = [
        {"manager": "argocd-controller", "operation": "Apply", "time": "2026-10-10T12:00:00Z", "fieldsType": "FieldsV1",
         "fieldsV1": {"f:spec": {"f:maxRunners": {}, "f:template": {"f:spec": {"f:containers": {'k:{"name":"runner"}': {"f:image": {}}}}}}}},
        {"manager": "kubectl-edit", "operation": "Update", "time": "2026-10-17T15:30:00Z", "fieldsType": "FieldsV1",
         "fieldsV1": {"f:spec": {"f:template": {"f:spec": {"f:containers": {'k:{"name":"runner"}': {"f:env": {'k:{"name":"RUNNER_DEBUG"}': {".": {}, "f:value": {}}}}}}}}}},
    ]
    objs.append(docker_live)
    baseline_doc = {"apiVersion": "actions.github.com/v1alpha1", "kind": "AutoscalingRunnerSet",
                    "metadata": {"name": "docker-builds", "namespace": ns, "labels": dlabels},
                    "spec": docker_baseline["spec"],
                    "_arcIntel": {"capturedAt": "2026-10-10T12:05:00Z", "reason": "Initial approval CHG-1182", "by": "arc-intel"}}
    objs.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": BASELINE_CONFIGMAP, "namespace": "arc-intel"},
                 "data": {f"{ns}.docker-builds": json.dumps(baseline_doc)}})

    # --- arc-runners/arm64-builds: Argo CD-managed, CPU limit raised by hand -------------------------------
    ns = "arc-runners"
    arm_spec = copy.deepcopy(base_spec)
    arm_spec["maxRunners"] = 10
    arm_target = _ars(ns, "arm64-builds", arm_spec, {
        "app.kubernetes.io/version": VERSION, "owner": "mobile", "cost-center": "cc-3310"})
    arm_target.pop("status")
    arm_target["metadata"] = {k: arm_target["metadata"][k] for k in ("name", "namespace", "labels")}
    arm_live = copy.deepcopy(_ars(ns, "arm64-builds", copy.deepcopy(arm_spec), dict(arm_target["metadata"]["labels"])))
    arm_live["metadata"]["annotations"] = {
        "argocd.argoproj.io/tracking-id": "arc-runners-arm64:actions.github.com/AutoscalingRunnerSet:arc-runners/arm64-builds"}
    arm_live["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "8"
    arm_live["metadata"]["managedFields"] = [
        {"manager": "argocd-controller", "operation": "Apply", "time": "2026-10-15T08:00:00Z", "fieldsType": "FieldsV1",
         "fieldsV1": {"f:spec": {"f:maxRunners": {}}}},
        {"manager": "kubectl-edit", "operation": "Update", "time": "2026-10-17T18:20:31Z", "fieldsType": "FieldsV1",
         "fieldsV1": {"f:spec": {"f:template": {"f:spec": {"f:containers": {'k:{"name":"runner"}': {
             "f:resources": {"f:limits": {"f:cpu": {}}}}}}}}}},
    ]
    objs.append(arm_live)
    objs.append({"kind": "ArgoTargetState", "app": "arc-runners-arm64", "namespace": ns, "name": "arm64-builds",
                 "target": arm_target})
    objs.append({
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": "arc-runners-arm64", "namespace": "argocd"},
        "spec": {"project": "arc-runners",
                 "source": {"repoURL": "https://github.com/my-org/platform-gitops", "path": "runner-scale-sets/arm64-builds",
                            "targetRevision": "main"},
                 "syncPolicy": {"automated": {"prune": True, "selfHeal": False}}},
        "status": {"sync": {"status": "OutOfSync", "revision": "9f3c2ab"}, "health": {"status": "Healthy"},
                   "resources": [{"group": "actions.github.com", "kind": "AutoscalingRunnerSet", "namespace": ns,
                                  "name": "arm64-builds", "status": "OutOfSync"}],
                   "operationState": {"phase": "Succeeded", "finishedAt": "2026-10-15T08:00:04Z",
                                      "operation": {"initiatedBy": {"automated": True}}}},
    })

    # --- arc-runners-legacy: everything wrong -------------------------------------------------------------
    ns = "arc-runners-legacy"
    objs.append({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "legacy-pat", "namespace": ns},
                 "data": {"github_token": "Z2hwX2R1bW15"}})
    objs.append(_ars(ns, "legacy-build", {
        "githubConfigUrl": "https://github.com/acquired-co", "githubConfigSecret": "legacy-pat", "minRunners": 8,
        "template": {"spec": {
            "hostNetwork": True,
            "volumes": [{"name": "docker-sock", "hostPath": {"path": "/var/run/docker.sock"}}],
            "containers": [
                {"name": "runner", "image": "ghcr.io/actions/actions-runner:2.311.0"},
                {"name": "dind", "image": "docker:dind", "securityContext": {"privileged": True}},
            ]}},
    }, labels={"app.kubernetes.io/version": "0.11.0"}))
    return objs


def dump(path: str) -> None:
    import yaml

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Generated by mcp-server/tests/fixtures.py — offline ARC cluster snapshot for demos.\n")
        yaml.safe_dump_all(build(), fh, sort_keys=False, width=200)


if __name__ == "__main__":
    dump(sys.argv[1] if len(sys.argv) > 1 else "cluster.yaml")

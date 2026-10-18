"""Cluster access layer.

`KubeCluster` talks to the API server (in-cluster or kubeconfig).
`StaticCluster` serves the same interface from YAML files, so the MCP server
and the agent can be demoed and tested without a live cluster.

Secret handling: only key *names* are ever returned for GitHub config secrets.
Helm release secrets are decoded server-side and only the AutoscalingRunnerSet
document is extracted; any Secret rendered by the chart is discarded.
"""

from __future__ import annotations

import base64
import glob
import gzip
import json
import logging
import os
from typing import Any, Protocol

import yaml

log = logging.getLogger(__name__)

GROUP, VERSION = "actions.github.com", "v1alpha1"
# Charts label the controller differently across versions; try both.
CONTROLLER_SELECTORS = ("app.kubernetes.io/part-of=gha-rs-controller", "app.kubernetes.io/name=gha-rs-controller")
BASELINE_CONFIGMAP = "arc-intel-baselines"


class Cluster(Protocol):
    def list_ars(self, namespace: str | None = None) -> list[dict]: ...
    def get_ars(self, namespace: str, name: str) -> dict | None: ...
    def secret_keys(self, namespace: str, name: str) -> tuple[bool | None, list[str] | None]: ...
    def controllers(self) -> list[dict]: ...
    def network_policy_count(self, namespace: str) -> int | None: ...
    def helm_release_manifest(self, namespace: str, release: str) -> str | None: ...
    def ephemeral_runner_stats(self, namespace: str, scale_set: str) -> dict: ...
    def get_baseline(self, namespace: str, name: str) -> dict | None: ...
    def put_baseline(self, namespace: str, name: str, doc: dict) -> None: ...
    def argocd_application(self, namespace: str, name: str) -> dict | None: ...


# ----------------------------------------------------------------------------- helm

def decode_helm_release(b64_data: str) -> dict:
    """Decode the `release` key of a helm.sh/release.v1 Secret (as returned by the API: base64 of base64(gzip(json)))."""
    raw = base64.b64decode(base64.b64decode(b64_data))
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def encode_helm_release(release: dict) -> str:
    """Inverse of decode_helm_release (used for fixtures/tests)."""
    return base64.b64encode(base64.b64encode(gzip.compress(json.dumps(release).encode()))).decode()


def ars_from_manifest(manifest: str, name: str) -> dict | None:
    for doc in yaml.safe_load_all(manifest or ""):
        if isinstance(doc, dict) and doc.get("kind") == "AutoscalingRunnerSet" and (doc.get("metadata") or {}).get("name") == name:
            return doc
    return None


def baseline_key(namespace: str, name: str) -> str:
    return f"{namespace}.{name}"


def strip_runtime(obj: dict) -> dict:
    """Keep only what a desired-state snapshot needs."""
    meta = obj.get("metadata") or {}
    return {
        "apiVersion": obj.get("apiVersion"),
        "kind": obj.get("kind"),
        "metadata": {k: meta[k] for k in ("name", "namespace", "labels", "annotations") if k in meta},
        "spec": obj.get("spec") or {},
    }


# ----------------------------------------------------------------------------- live cluster

class KubeCluster:
    def __init__(self, own_namespace: str | None = None):
        from kubernetes import client, config  # imported lazily so tests don't need it

        try:
            config.load_incluster_config()
        except Exception:  # noqa: BLE001
            config.load_kube_config()
        self.client = client
        self.api = client.ApiClient()
        self.custom = client.CustomObjectsApi()
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.net = client.NetworkingV1Api()
        self.own_ns = own_namespace or os.environ.get("POD_NAMESPACE", "arc-intel")

    def _plain(self, obj: Any) -> dict:
        return self.api.sanitize_for_serialization(obj)

    def list_ars(self, namespace=None):
        if namespace:
            res = self.custom.list_namespaced_custom_object(GROUP, VERSION, namespace, "autoscalingrunnersets")
        else:
            res = self.custom.list_cluster_custom_object(GROUP, VERSION, "autoscalingrunnersets")
        return res.get("items", [])

    def get_ars(self, namespace, name):
        try:
            return self.custom.get_namespaced_custom_object(GROUP, VERSION, namespace, "autoscalingrunnersets", name)
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def secret_keys(self, namespace, name):
        try:
            s = self.core.read_namespaced_secret(name, namespace)
            return True, sorted((s.data or {}).keys())
        except self.client.ApiException as e:
            if e.status == 404:
                return False, None
            if e.status == 403:
                return None, None  # not permitted: unknown
            raise

    def controllers(self):
        out, seen = [], set()
        deployments = []
        for sel in CONTROLLER_SELECTORS:
            deployments += self.apps.list_deployment_for_all_namespaces(label_selector=sel).items
        for d in deployments:
            key = (d.metadata.namespace, d.metadata.name)
            if key in seen:
                continue
            seen.add(key)
            labels = d.metadata.labels or {}
            image = d.spec.template.spec.containers[0].image if d.spec.template.spec.containers else None
            # Chart version label, else the controller image tag (gha-runner-scale-set-controller:<version>).
            version = labels.get("app.kubernetes.io/version") or (image.rsplit(":", 1)[-1] if image and ":" in image else None)
            out.append({
                "namespace": d.metadata.namespace,
                "name": d.metadata.name,
                "version": version,
                "image": image,
                "ready": f"{d.status.ready_replicas or 0}/{d.spec.replicas or 0}",
            })
        return out

    def network_policy_count(self, namespace):
        try:
            return len(self.net.list_namespaced_network_policy(namespace).items)
        except self.client.ApiException:
            return None

    def helm_release_manifest(self, namespace, release):
        sel = f"owner=helm,name={release},status=deployed"
        try:
            items = self.core.list_namespaced_secret(namespace, label_selector=sel).items
        except self.client.ApiException:
            return None
        if not items:
            return None
        latest = max(items, key=lambda s: int((s.metadata.labels or {}).get("version", "0")))
        return decode_helm_release(latest.data["release"]).get("manifest")

    def ephemeral_runner_stats(self, namespace, scale_set):
        try:
            res = self.custom.list_namespaced_custom_object(
                GROUP, VERSION, namespace, "ephemeralrunners",
                label_selector=f"actions.github.com/scale-set-name={scale_set}")
        except self.client.ApiException:
            return {}
        stats: dict[str, int] = {}
        for er in res.get("items", []):
            phase = (er.get("status") or {}).get("phase") or "Pending"
            stats[phase] = stats.get(phase, 0) + 1
        return stats

    def argocd_application(self, namespace, name):
        try:
            return self.custom.get_namespaced_custom_object("argoproj.io", "v1alpha1", namespace, "applications", name)
        except self.client.ApiException as e:
            if e.status in (403, 404):
                return None
            raise

    def _baseline_cm(self):
        try:
            return self.core.read_namespaced_config_map(BASELINE_CONFIGMAP, self.own_ns)
        except self.client.ApiException as e:
            if e.status == 404:
                return None
            raise

    def get_baseline(self, namespace, name):
        cm = self._baseline_cm()
        raw = ((cm.data or {}) if cm else {}).get(baseline_key(namespace, name))
        return json.loads(raw) if raw else None

    def put_baseline(self, namespace, name, doc):
        body = {baseline_key(namespace, name): json.dumps(doc)}
        cm = self._baseline_cm()
        if cm is None:
            self.core.create_namespaced_config_map(self.own_ns, self.client.V1ConfigMap(
                metadata=self.client.V1ObjectMeta(name=BASELINE_CONFIGMAP, labels={"app.kubernetes.io/part-of": "arc-intel"}),
                data=body))
        else:
            self.core.patch_namespaced_config_map(BASELINE_CONFIGMAP, self.own_ns, {"data": body})


# ----------------------------------------------------------------------------- static (offline)

class StaticCluster:
    """Reads Kubernetes objects from *.yaml files in a directory."""

    def __init__(self, path: str):
        self.objs: list[dict] = []
        files = [path] if os.path.isfile(path) else sorted(glob.glob(os.path.join(path, "*.y*ml")))
        for f in files:
            with open(f, encoding="utf-8") as fh:
                self.objs += [d for d in yaml.safe_load_all(fh) if isinstance(d, dict)]
        self.baselines: dict[str, dict] = {}
        for o in self._kind("ConfigMap"):
            if o["metadata"]["name"] == BASELINE_CONFIGMAP:
                self.baselines.update({k: json.loads(v) for k, v in (o.get("data") or {}).items()})

    def _kind(self, kind, namespace=None):
        return [o for o in self.objs if o.get("kind") == kind
                and (namespace is None or (o.get("metadata") or {}).get("namespace") == namespace)]

    def list_ars(self, namespace=None):
        return self._kind("AutoscalingRunnerSet", namespace)

    def get_ars(self, namespace, name):
        return next((o for o in self.list_ars(namespace) if o["metadata"]["name"] == name), None)

    def secret_keys(self, namespace, name):
        s = next((o for o in self._kind("Secret", namespace) if o["metadata"]["name"] == name), None)
        if s is None:
            return False, None
        return True, sorted(set((s.get("data") or {}).keys()) | set((s.get("stringData") or {}).keys()))

    def controllers(self):
        out = []
        for d in self._kind("Deployment"):
            labels = d["metadata"].get("labels") or {}
            if "gha-rs-controller" in (labels.get("app.kubernetes.io/part-of"), labels.get("app.kubernetes.io/name")):
                c = (((d.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or [{}]
                out.append({"namespace": d["metadata"].get("namespace"), "name": d["metadata"]["name"],
                            "version": labels.get("app.kubernetes.io/version"), "image": c[0].get("image"),
                            "ready": "1/1"})
        return out

    def network_policy_count(self, namespace):
        return len(self._kind("NetworkPolicy", namespace))

    def helm_release_manifest(self, namespace, release):
        for s in self._kind("Secret", namespace):
            labels = s["metadata"].get("labels") or {}
            if s.get("type") == "helm.sh/release.v1" and labels.get("name") == release and labels.get("status") == "deployed":
                return decode_helm_release(s["data"]["release"]).get("manifest")
        return None

    def ephemeral_runner_stats(self, namespace, scale_set):
        stats: dict[str, int] = {}
        for er in self._kind("EphemeralRunner", namespace):
            if (er["metadata"].get("labels") or {}).get("actions.github.com/scale-set-name") == scale_set:
                phase = (er.get("status") or {}).get("phase") or "Pending"
                stats[phase] = stats.get(phase, 0) + 1
        return stats

    def argocd_application(self, namespace, name):
        return next((o for o in self._kind("Application", namespace) if o["metadata"]["name"] == name), None)

    def get_baseline(self, namespace, name):
        return self.baselines.get(baseline_key(namespace, name))

    def put_baseline(self, namespace, name, doc):
        self.baselines[baseline_key(namespace, name)] = doc


def from_env() -> Cluster:
    offline = os.environ.get("ARC_INTEL_OFFLINE_DIR")
    if offline:
        log.info("Using offline cluster snapshot at %s", offline)
        return StaticCluster(offline)
    return KubeCluster()

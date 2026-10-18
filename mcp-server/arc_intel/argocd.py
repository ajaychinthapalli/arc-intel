"""Argo CD as the source of truth for runner scale sets.

Argo CD renders Helm charts itself (`helm template`), so there is no Helm release
Secret to diff against. Instead:

* ownership comes from the tracking annotation (`argocd.argoproj.io/tracking-id`,
  the default in Argo CD 3.x) or the instance label (label tracking);
* desired state comes from Argo CD's API: `GET /api/v1/applications/{app}/managed-resources`
  returns the rendered target manifest per resource;
* sync context (sync status, health, automated sync, self-heal, last operation and who
  started it, source repo/path/revision) comes from the Application custom resource.

arc-intel authenticates to the Argo CD API with a read-only local account token
(`applications, get` only) mounted from a Secret.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"


def owning_application(ars: dict, default_app_namespace: str = "argocd") -> tuple[str, str] | None:
    """(application name, application namespace) that manages this object, if Argo CD tracks it.

    tracking-id format: `<app>:<group>/<Kind>:<namespace>/<name>`; with apps in any
    namespace the app part is `<appNamespace>_<app>`.
    """
    meta = ars.get("metadata") or {}
    tid = (meta.get("annotations") or {}).get(TRACKING_ANNOTATION)
    if tid and ":" in tid:
        app = tid.split(":", 1)[0]
        if "_" in app:
            ns, app = app.split("_", 1)
            return app, ns
        return app, default_app_namespace
    # Label tracking. `app.kubernetes.io/instance` alone is ambiguous (Helm sets it too), so it
    # only counts when Argo CD is configured to use it: ARGOCD_INSTANCE_LABEL=app.kubernetes.io/instance.
    labels = meta.get("labels") or {}
    key = os.environ.get("ARGOCD_INSTANCE_LABEL", "argocd.argoproj.io/instance")
    if labels.get(key):
        return labels[key], default_app_namespace
    return None


def application_context(app: dict | None, ars: dict) -> dict[str, Any]:
    """Summarise an Application CR for the agent: what Git says, what Argo did, who did it."""
    if not app:
        return {"found": False}
    spec, st = app.get("spec") or {}, app.get("status") or {}
    src = spec.get("source") or ((spec.get("sources") or [{}])[0])
    auto = ((spec.get("syncPolicy") or {}).get("automated"))
    op = st.get("operationState") or {}
    initiated = (op.get("operation") or {}).get("initiatedBy") or {}
    meta = ars.get("metadata") or {}
    res = next((r for r in st.get("resources") or []
                if r.get("kind") == "AutoscalingRunnerSet" and r.get("name") == meta.get("name")
                and r.get("namespace") == meta.get("namespace")), {})
    return {
        "found": True,
        "application": (app.get("metadata") or {}).get("name"),
        "project": spec.get("project"),
        "source": {k: src.get(k) for k in ("repoURL", "path", "chart", "targetRevision") if src.get(k)},
        "sync_status": (st.get("sync") or {}).get("status"),
        "synced_revision": (st.get("sync") or {}).get("revision"),
        "health": (st.get("health") or {}).get("status"),
        "resource_status": res.get("status"),
        "automated_sync": auto is not None,
        "self_heal": bool(auto and (auto.get("selfHeal") if isinstance(auto, dict) else False)),
        "prune": bool(auto and (auto.get("prune") if isinstance(auto, dict) else False)),
        "last_operation": {
            "phase": op.get("phase"),
            "finished_at": op.get("finishedAt"),
            "initiated_by": initiated.get("username") or ("automated" if initiated.get("automated") else None),
            "message": op.get("message"),
        } if op else None,
    }


class ArgoCDClient:
    def __init__(self, base_url: str, token: str, verify_tls: bool = True, timeout: float = 15.0):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.ctx = None if verify_tls else ssl._create_unverified_context()  # noqa: S323 - opt-in for internal CA setups

    def _get(self, path: str, params: dict[str, str]) -> Any:
        url = f"{self.base}{path}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:  # noqa: S310 - fixed scheme from config
            return json.loads(r.read() or b"{}")

    def target_state(self, app: str, app_namespace: str, namespace: str, name: str,
                     group: str = "actions.github.com", kind: str = "AutoscalingRunnerSet") -> dict | None:
        data = self._get(f"/api/v1/applications/{urllib.parse.quote(app)}/managed-resources", {
            "appNamespace": app_namespace, "namespace": namespace, "name": name, "group": group, "kind": kind,
        })
        for item in data.get("items") or []:
            if item.get("kind") == kind and item.get("name") == name and item.get("namespace") == namespace:
                raw = item.get("targetState")
                return json.loads(raw) if raw else None
        return None


class StaticArgoCD:
    """Offline stand-in: target states from `ArgoTargetState` docs in a snapshot (demo/tests only)."""

    def __init__(self, objs: list[dict]):
        self.targets = {
            (o["app"], o["namespace"], o["name"]): o["target"] for o in objs if o.get("kind") == "ArgoTargetState"
        }

    def target_state(self, app, app_namespace, namespace, name, group="actions.github.com", kind="AutoscalingRunnerSet"):
        return self.targets.get((app, namespace, name))


def from_env(cluster: Any = None) -> Any:
    if os.environ.get("ARC_INTEL_OFFLINE_DIR") and cluster is not None and hasattr(cluster, "objs"):
        return StaticArgoCD(cluster.objs)
    url, token_file = os.environ.get("ARGOCD_SERVER_URL"), os.environ.get("ARGOCD_TOKEN_FILE", "/var/run/argocd/token")
    if not url or not os.path.exists(token_file):
        return None
    with open(token_file, encoding="utf-8") as fh:
        token = fh.read().strip()
    return ArgoCDClient(url, token, verify_tls=os.environ.get("ARGOCD_INSECURE", "false").lower() != "true")

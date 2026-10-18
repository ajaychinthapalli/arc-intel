"""Policy loading and per-scale-set settings resolution."""

from __future__ import annotations

import copy
import fnmatch
import os
from dataclasses import dataclass, field
from typing import Any

import yaml

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEVERITY_WEIGHT = {"info": 0, "low": 2, "medium": 5, "high": 10, "critical": 25}

DEFAULT_SETTINGS: dict[str, Any] = {
    "allowedGithubConfigUrlPrefixes": [],
    "allowedRegistries": ["ghcr.io/actions/"],
    "maxRunnersCeiling": 50,
    "maxMinRunners": 5,
    "minRunnerVersion": "2.320.0",
    "requireGithubApp": True,
    "allowDind": False,
    "forbidDefaultRunnerGroup": True,
    "requiredLabels": [],
    "requireNetworkPolicy": True,
    "expectedManagers": ["helm", "argocd-controller", "helm-controller", "kustomize-controller", "manager"],
    # Kubernetes usernames (fnmatch) allowed to write scale sets; anything else in the EKS audit log
    # is an out-of-band change, even if Argo CD self-heal has already reverted it.
    "expectedWriterIdentities": [
        "system:serviceaccount:argocd:argocd-application-controller",
        "system:serviceaccount:arc-systems:*",
    ],
}


@dataclass
class Policy:
    settings: dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULT_SETTINGS))
    overrides: list[dict[str, Any]] = field(default_factory=list)
    rules: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Policy":
        data = data or {}
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        settings.update(data.get("settings") or {})
        return cls(
            settings=settings,
            overrides=list(data.get("overrides") or []),
            rules=dict(data.get("rules") or {}),
        )

    @classmethod
    def load(cls, path: str | None = None) -> "Policy":
        path = path or os.environ.get("ARC_INTEL_POLICY", "/etc/arc-intel/policy.yaml")
        if not os.path.exists(path):
            return cls.from_dict({})
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(yaml.safe_load(fh))

    def settings_for(self, ars: dict[str, Any]) -> dict[str, Any]:
        """Resolve settings for one AutoscalingRunnerSet, applying overrides in order."""
        meta = ars.get("metadata") or {}
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        labels = meta.get("labels") or {}
        resolved = copy.deepcopy(self.settings)
        for ov in self.overrides:
            match = ov.get("match") or {}
            if "namespace" in match and not fnmatch.fnmatch(ns, match["namespace"]):
                continue
            if "name" in match and not fnmatch.fnmatch(name, match["name"]):
                continue
            if any(labels.get(k) != v for k, v in (match.get("labels") or {}).items()):
                continue
            resolved.update(ov.get("settings") or {})
        return resolved

    def rule_config(self, rule_id: str) -> dict[str, Any]:
        return self.rules.get(rule_id) or {}

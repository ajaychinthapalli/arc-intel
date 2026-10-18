"""Combines compliance and drift into a per-scale-set posture and fleet report."""

from __future__ import annotations

import datetime as dt
import fnmatch
import logging
from typing import Any

from . import drift as D
from . import rules as R
from .argocd import application_context, owning_application
from .cluster import Cluster, ars_from_manifest, strip_runtime

log = logging.getLogger(__name__)
from .policy import SEVERITY_ORDER, SEVERITY_WEIGHT, Policy

# Posture quadrants: is the *desired* state compliant, and does live match it?
POSTURE_HELP = {
    "healthy": "Compliant and matches its source of truth.",
    "drifted": "Compliant, but live differs from the source of truth. Revert or upstream the change.",
    "non_compliant": "Source of truth itself violates policy. Fix it in Git/Helm values.",
    "drift_introduced_violation": "A live change created a policy violation that the source of truth does not have. Treat as an incident.",
    "unknown_baseline": "No source of truth found, so drift cannot be assessed.",
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _ref(ars: dict) -> tuple[str, str]:
    m = ars.get("metadata") or {}
    return m.get("namespace", ""), m.get("name", "")


def score(findings: list[R.Finding]) -> int:
    return max(0, 100 - sum(SEVERITY_WEIGHT.get(f.severity, 0) for f in findings))


def grade(s: int) -> str:
    return "A" if s >= 90 else "B" if s >= 75 else "C" if s >= 60 else "D" if s >= 40 else "F"


class Engine:
    def __init__(self, cluster: Cluster, policy: Policy, audit: Any = None, audit_lookback_hours: int = 168,
                 argocd: Any = None, argocd_namespace: str = "argocd"):
        self.cluster = cluster
        self.policy = policy
        self.audit = audit  # optional CloudWatchAudit (EKS): resolves field managers to IAM identities
        self.audit_lookback_hours = audit_lookback_hours
        self.argocd = argocd  # optional Argo CD API client: rendered target state per resource
        self.argocd_namespace = argocd_namespace
        self._controllers: list[dict] | None = None

    # ------------------------------------------------------------------ facts

    def controllers(self, refresh: bool = False) -> list[dict]:
        if self._controllers is None or refresh:
            self._controllers = self.cluster.controllers()
        return self._controllers

    def facts(self, ars: dict) -> R.Facts:
        ns, _ = _ref(ars)
        secret = (ars.get("spec") or {}).get("githubConfigSecret")
        exists, keys = self.cluster.secret_keys(ns, secret) if secret else (None, None)
        versions = sorted({c["version"] for c in self.controllers() if c.get("version")})
        return R.Facts(secret_keys=keys, secret_exists=exists,
                       controller_versions=versions, network_policy_count=self.cluster.network_policy_count(ns))

    # ------------------------------------------------------------------ compliance

    def evaluate(self, ars: dict) -> list[R.Finding]:
        settings = self.policy.settings_for(ars)
        found = R.evaluate(ars, settings, self.facts(ars), self.policy.rules)
        return sorted(found, key=lambda f: -SEVERITY_ORDER.get(f.severity, 0))

    # ------------------------------------------------------------------ desired state

    def desired_state(self, ars: dict, source: str = "auto") -> tuple[dict | None, str]:
        ns, name = _ref(ars)
        meta = ars.get("metadata") or {}
        if source in ("auto", "baseline"):
            doc = self.cluster.get_baseline(ns, name)
            if doc:
                return doc, "baseline"
        if source in ("auto", "argocd"):
            owner = owning_application(ars, self.argocd_namespace)
            if owner and self.argocd is not None:
                try:
                    doc = self.argocd.target_state(owner[0], owner[1], ns, name)
                    if doc:
                        return doc, f"argocd:{owner[0]}"
                except Exception as e:  # noqa: BLE001 - fall through to the next source
                    log.warning("Argo CD target state for %s/%s failed: %s", ns, name, e)
        if source in ("auto", "helm"):
            release = (meta.get("annotations") or {}).get("meta.helm.sh/release-name") \
                or (meta.get("labels") or {}).get("app.kubernetes.io/instance")
            if release:
                manifest = self.cluster.helm_release_manifest(ns, release)
                doc = ars_from_manifest(manifest, name) if manifest else None
                if doc:
                    return doc, f"helm:{release}"
        if source in ("auto", "last-applied"):
            import json

            raw = (meta.get("annotations") or {}).get("kubectl.kubernetes.io/last-applied-configuration")
            if raw:
                return json.loads(raw), "last-applied"
        return None, "none"

    def gitops_context(self, ars: dict) -> dict[str, Any] | None:
        owner = owning_application(ars, self.argocd_namespace)
        if owner:
            ctx = application_context(self.cluster.argocd_application(owner[1], owner[0]), ars)
            return {"controller": "argocd", "application": owner[0], "application_namespace": owner[1], **ctx}
        labels = (ars.get("metadata") or {}).get("labels") or {}
        if any("fluxcd" in k for k in labels):
            return {"controller": "flux"}
        return None

    def drift(self, ars: dict, source: str = "auto", with_audit: bool = False) -> dict[str, Any]:
        ns, name = _ref(ars)
        settings = self.policy.settings_for(ars)
        gitops = self.gitops_context(ars)

        # Audit trail (EKS): fetched once, used for field attribution and for spotting
        # out-of-band writes that Argo CD self-heal has already reverted.
        events, identity = None, "not_requested"
        if with_audit:
            if self.audit is None:
                identity = "unavailable (EKS audit lookup not configured)"
            else:
                try:
                    events = self.audit.trail(ns, name, hours=self.audit_lookback_hours)
                    identity = "eks_audit_log"
                except Exception as e:  # noqa: BLE001 - attribution is best effort
                    identity = f"error: {e}"
        out_of_band = None
        if events is not None:
            allowed = settings.get("expectedWriterIdentities") or []
            out_of_band = [e for e in events if not any(fnmatch.fnmatch(e.get("username") or "", p) for p in allowed)]

        desired, src = self.desired_state(ars, source)
        if desired is None:
            return {"namespace": ns, "name": name, "baseline_source": "none", "drifted": None, "entries": [],
                    "gitops": gitops, "identity_source": identity,
                    **({"out_of_band_writes": out_of_band} if out_of_band else {}),
                    "note": "No baseline, Argo CD target, Helm release or last-applied config found. "
                            "Capture a baseline, or grant arc-intel read access to the Argo CD API."}
        entries = D.diff_ars(desired, ars)
        D.attribute(entries, (ars.get("metadata") or {}).get("managedFields") or [], settings.get("expectedManagers") or [])
        entries.sort(key=lambda e: -SEVERITY_ORDER.get(e.severity, 0))
        if events:
            from .audit import correlate

            correlate(entries, events)
        result = {
            "namespace": ns, "name": name, "baseline_source": src, "drifted": bool(entries),
            "summary": {s: sum(1 for e in entries if e.severity == s) for s in ("critical", "high", "medium", "low")},
            "manual_changes": sum(1 for e in entries if e.manual_change),
            "identity_source": identity,
            "gitops": gitops,
            "gitops_controller": (gitops or {}).get("controller"),
            "entries": [e.to_dict() for e in entries],
            "_entries": entries,
            "_desired": desired,
        }
        if out_of_band:
            result["out_of_band_writes"] = out_of_band
            # Live matches Git, yet someone outside the GitOps path wrote to it: self-heal hid the change.
            result["self_healed"] = not entries and bool((gitops or {}).get("self_heal"))
        return result

    # ------------------------------------------------------------------ posture

    def assess(self, ars: dict, with_drift: bool = True, with_audit: bool = False) -> dict[str, Any]:
        ns, name = _ref(ars)
        live_findings = self.evaluate(ars)
        result: dict[str, Any] = {"namespace": ns, "name": name}
        posture = "healthy" if not live_findings else "non_compliant"
        drift_info = None
        if with_drift:
            drift_info = self.drift(ars, with_audit=with_audit)
            if drift_info["baseline_source"] == "none":
                posture = "unknown_baseline" if not live_findings else "non_compliant"
            else:
                # Which live violations does the desired state NOT have? Those were introduced by drift.
                desired = drift_info["_desired"]
                desired_full = {**desired, "metadata": {**(ars.get("metadata") or {}), **(desired.get("metadata") or {})}}
                desired_ids = {(f.rule_id, f.path) for f in self.evaluate(desired_full)}
                for f in live_findings:
                    f.origin = "source_of_truth" if (f.rule_id, f.path) in desired_ids else "drift"
                if any(f.origin == "drift" for f in live_findings):
                    posture = "drift_introduced_violation"
                elif live_findings:
                    posture = "non_compliant"
                elif drift_info["drifted"]:
                    posture = "drifted"
        s = score(live_findings)
        result.update({
            "posture": posture, "posture_meaning": POSTURE_HELP[posture],
            "score": s, "grade": grade(s),
            "violations": [f.to_dict() for f in live_findings],
        })
        if drift_info is not None:
            result["drift"] = {k: v for k, v in drift_info.items() if not k.startswith("_")}
        return result

    # ------------------------------------------------------------------ fleet

    def inventory(self, namespace: str | None = None) -> list[dict]:
        rows = []
        for ars in self.cluster.list_ars(namespace):
            ns, name = _ref(ars)
            sp, st = ars.get("spec") or {}, ars.get("status") or {}
            runner = R.runner_container(ars) or {}
            rows.append({
                "namespace": ns, "name": name,
                "runs_on": sp.get("runnerScaleSetName") or name,
                "github": sp.get("githubConfigUrl"), "runner_group": sp.get("runnerGroup"),
                "min": sp.get("minRunners"), "max": sp.get("maxRunners"),
                "image": runner.get("image"), "mode": "dind" if R.is_dind(ars) else
                ("kubernetes" if any(e.get("name") == "ACTIONS_RUNNER_CONTAINER_HOOKS" for e in runner.get("env") or []) else "default"),
                "chart_version": ((ars.get("metadata") or {}).get("labels") or {}).get("app.kubernetes.io/version"),
                "status": {k: st.get(k) for k in ("currentRunners", "pendingEphemeralRunners", "runningEphemeralRunners", "failedEphemeralRunners") if k in st},
                "ephemeral_runners": self.cluster.ephemeral_runner_stats(ns, name),
            })
        return rows

    def fleet_report(self, namespace: str | None = None) -> dict[str, Any]:
        assessed = [self.assess(a) for a in self.cluster.list_ars(namespace)]
        by_rule: dict[str, dict] = {}
        for a in assessed:
            for v in a["violations"]:
                r = by_rule.setdefault(v["rule_id"], {"rule_id": v["rule_id"], "title": v["title"], "severity": v["severity"], "scale_sets": []})
                r["scale_sets"].append(f"{a['namespace']}/{a['name']}")
        postures: dict[str, int] = {}
        for a in assessed:
            postures[a["posture"]] = postures.get(a["posture"], 0) + 1
        fleet_score = round(sum(a["score"] for a in assessed) / len(assessed)) if assessed else 100
        return {
            "generated_at": _now(),
            "scale_sets": len(assessed),
            "fleet_score": fleet_score, "fleet_grade": grade(fleet_score),
            "postures": postures,
            "controllers": self.controllers(refresh=True),
            "top_risks": sorted(by_rule.values(), key=lambda r: (-SEVERITY_ORDER[r["severity"]], -len(r["scale_sets"])))[:10],
            "scale_set_summaries": [
                {"ref": f"{a['namespace']}/{a['name']}", "posture": a["posture"], "score": a["score"], "grade": a["grade"],
                 "critical": sum(1 for v in a["violations"] if v["severity"] == "critical"),
                 "drift_fields": len((a.get("drift") or {}).get("entries", [])),
                 "manual_changes": (a.get("drift") or {}).get("manual_changes", 0)}
                for a in sorted(assessed, key=lambda a: a["score"])
            ],
        }

    # ------------------------------------------------------------------ remediation

    @staticmethod
    def _revert_guidance(d: dict) -> str:
        src, g = d["baseline_source"], d.get("gitops") or {}
        if src.startswith("argocd:"):
            app = src.split(":", 1)[1]
            if g.get("self_heal"):
                return (f"Argo CD application '{app}' has self-heal on, so this drift should already be reverted. "
                        f"If it persists, check the app's sync errors or ignoreDifferences covering these fields.")
            return (f"Revert by syncing Argo CD application '{app}' (`argocd app sync {app}`); "
                    f"if the change is wanted, make it in Git instead. Avoid patching: the next sync overwrites it.")
        if src.startswith("helm:"):
            return (f"Managed by Helm release '{src.split(':', 1)[1]}': prefer `helm upgrade` with the committed values; "
                    "a direct patch will be overwritten on next upgrade.")
        if g.get("controller"):
            return "GitOps controller present: a sync will revert this; investigate why it hasn't."
        return "Patch includes `test` ops and fails safely if the field changed again."

    def remediation(self, ars: dict) -> dict[str, Any]:
        ns, name = _ref(ars)
        d = self.drift(ars)
        out: dict[str, Any] = {"namespace": ns, "name": name, "apply_automatically": False}
        if d.get("_entries"):
            patch = D.remediation_patch(d["_entries"])
            out["drift_revert"] = {
                "baseline_source": d["baseline_source"],
                "json_patch": patch,
                "kubectl": f"kubectl -n {ns} patch autoscalingrunnerset {name} --type=json -p '<json_patch>'",
                "guidance": self._revert_guidance(d),
            }
        findings = self.evaluate(ars)
        g = d.get("gitops") or {}
        if findings and g.get("controller") == "argocd":
            src = g.get("source") or {}
            out["fix_in_git"] = {
                "application": g.get("application"), "repo": src.get("repoURL"),
                "path": src.get("path") or src.get("chart"), "revision": src.get("targetRevision"),
                "how": "Open a PR changing the scale-set values in this repo/path; Argo CD syncs it after merge.",
            }
        if findings:
            out["policy_fixes"] = [{"rule_id": f.rule_id, "severity": f.severity, "path": f.path, "fix": f.fix} for f in findings]
        if not d.get("_entries") and not findings:
            out["message"] = "Nothing to remediate."
        return out

    def capture_baseline(self, ars: dict, reason: str, actor: str = "arc-intel") -> dict[str, Any]:
        ns, name = _ref(ars)
        doc = strip_runtime(ars)
        doc["metadata"].setdefault("annotations", {})
        doc["metadata"]["annotations"] = {k: v for k, v in doc["metadata"]["annotations"].items()
                                          if not k.startswith(("kubectl.kubernetes.io/", "actions.github.com/", "runner-scale-set-id"))}
        doc["_arcIntel"] = {"capturedAt": _now(), "reason": reason, "by": actor,
                            "resourceVersion": (ars.get("metadata") or {}).get("resourceVersion")}
        self.cluster.put_baseline(ns, name, doc)
        return {"namespace": ns, "name": name, "captured": True, **doc["_arcIntel"]}

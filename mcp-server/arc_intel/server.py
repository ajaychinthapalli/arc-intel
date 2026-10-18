"""MCP server exposing ARC compliance & drift tools to Kagent (streamable HTTP)."""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import metrics
from .cluster import from_env
from .engine import POSTURE_HELP, Engine
from .policy import SEVERITY_ORDER, Policy
from .rules import describe_rules

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("arc-intel")

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)

mcp = MCPServer(
    name="arc-intel",
    version="0.1.0",
    instructions=(
        "Tools for GitHub Actions Runner Controller (ARC) configuration compliance and drift. "
        "All tools are read-only except arc_capture_baseline, which only writes to the arc-intel baseline ConfigMap. "
        "Remediation output is advisory: nothing is ever applied to runner scale sets."
    ),
)

_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        from . import argocd, audit

        cluster = from_env()
        _engine = Engine(cluster, Policy.load(), audit=audit.from_env(),
                         audit_lookback_hours=int(os.environ.get("AUDIT_LOOKBACK_HOURS", "168")),
                         argocd=argocd.from_env(cluster),
                         argocd_namespace=os.environ.get("ARGOCD_NAMESPACE", "argocd"))
    return _engine


def _get(namespace: str, name: str) -> dict:
    ars = engine().cluster.get_ars(namespace, name)
    if ars is None:
        raise ValueError(f"AutoscalingRunnerSet {namespace}/{name} not found")
    return ars


def _scope(namespace: str, name: str) -> list[dict]:
    if name:
        if not namespace:
            raise ValueError("namespace is required when name is given")
        return [_get(namespace, name)]
    return engine().cluster.list_ars(namespace or None)


@mcp.tool(annotations=READ_ONLY)
def arc_inventory(namespace: str = "") -> dict[str, Any]:
    """List ARC runner scale sets (AutoscalingRunnerSets) with GitHub scope, runner group, min/max runners,
    runner image, container mode, chart version and ephemeral runner health. Empty namespace = all namespaces."""
    rows = engine().inventory(namespace or None)
    return {"count": len(rows), "scale_sets": rows, "controllers": engine().controllers(refresh=True)}


@mcp.tool(annotations=READ_ONLY)
def arc_check_compliance(namespace: str = "", name: str = "", min_severity: str = "low") -> dict[str, Any]:
    """Evaluate runner scale sets against the ARC policy pack (auth, GitHub scope, scaling bounds, image pinning,
    registries, runner version, privileged/host access, resources, runner group, labels, NetworkPolicy,
    controller version skew, declarative management). Returns findings sorted by severity with a 0-100 score.
    min_severity: info|low|medium|high|critical."""
    floor = SEVERITY_ORDER.get(min_severity, 1)
    out = []
    for ars in _scope(namespace, name):
        a = engine().assess(ars, with_drift=False)
        a["violations"] = [v for v in a["violations"] if SEVERITY_ORDER.get(v["severity"], 0) >= floor]
        out.append(a)
    return {"results": out}


@mcp.tool(annotations=READ_ONLY)
def arc_detect_drift(namespace: str, name: str, baseline_source: str = "auto") -> dict[str, Any]:
    """Compare a live runner scale set to its source of truth and attribute every drifted field to the
    field manager (kubectl-edit, helm, argocd...) and time that wrote it. On EKS with audit logging, each
    writer also carries `actor`: the Kubernetes username, IAM ARN and session behind the change.
    Argo CD-managed sets include `gitops` (application, repo/path/revision, sync status, health, self-heal, last
    sync and who started it) and, on EKS, `out_of_band_writes` / `self_healed` for changes Argo already reverted.
    baseline_source: auto (captured baseline > Argo CD target > Helm release > last-applied) | baseline | argocd |
    helm | last-applied."""
    d = engine().drift(_get(namespace, name), baseline_source, with_audit=True)
    return {k: v for k, v in d.items() if not k.startswith("_")}


@mcp.tool(annotations=READ_ONLY)
def arc_assess(namespace: str, name: str) -> dict[str, Any]:
    """Full posture for one scale set: compliance + drift combined into one of
    healthy | drifted | non_compliant | drift_introduced_violation | unknown_baseline.
    Each violation is tagged with origin=drift (introduced by a live change) or origin=source_of_truth."""
    return engine().assess(_get(namespace, name), with_audit=True)


@mcp.tool(annotations=READ_ONLY)
def arc_audit_trail(namespace: str, name: str, hours: int = 24) -> dict[str, Any]:
    """EKS only: every successful create/update/patch/delete of one runner scale set from the EKS control-plane
    audit log (CloudWatch), with Kubernetes username, IAM role/user ARN, session name, user agent and source IP.
    hours: lookback window, max 720."""
    a = engine().audit
    if a is None:
        return {"available": False, "reason": "Set EKS_AUDIT_LOG_GROUP and enable EKS audit logging to use this tool."}
    events = a.trail(namespace, name, hours=hours)
    return {"available": True, "log_group": a.log_group, "count": len(events), "events": events}


@mcp.tool(annotations=READ_ONLY)
def arc_fleet_report(namespace: str = "") -> dict[str, Any]:
    """Fleet-wide posture: fleet score/grade, posture counts, controllers, top risks by rule, and per-scale-set
    summaries sorted worst first. Use this for daily digests and 'how are we doing' questions."""
    return engine().fleet_report(namespace or None)


@mcp.tool(annotations=READ_ONLY)
def arc_controller_health() -> dict[str, Any]:
    """ARC controller deployments (namespace, version, image, readiness) and any scale sets whose chart version
    does not match a controller version (unsupported skew)."""
    ctrls = engine().controllers(refresh=True)
    versions = {c.get("version") for c in ctrls}
    skew = [
        {"ref": f"{r['namespace']}/{r['name']}", "chart_version": r["chart_version"]}
        for r in engine().inventory() if r.get("chart_version") and r["chart_version"] not in versions
    ]
    return {"controllers": ctrls, "version_skew": skew, "healthy": bool(ctrls) and not skew}


@mcp.tool(annotations=READ_ONLY)
def arc_propose_remediation(namespace: str, name: str) -> dict[str, Any]:
    """Advisory remediation: a guarded RFC 6902 JSON patch that reverts drift (with `test` ops so it fails safely
    if the field changes again) plus per-rule fixes for policy violations. Never applied automatically."""
    return engine().remediation(_get(namespace, name))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
def arc_capture_baseline(namespace: str, name: str, reason: str) -> dict[str, Any]:
    """Record the scale set's current spec as its approved baseline (stored in the arc-intel-baselines ConfigMap).
    Only call after a human has confirmed the live state is approved. Requires ALLOW_BASELINE_WRITE=true."""
    if os.environ.get("ALLOW_BASELINE_WRITE", "false").lower() != "true":
        return {"captured": False, "error": "Baseline writes are disabled (ALLOW_BASELINE_WRITE != true)."}
    if not reason.strip():
        return {"captured": False, "error": "A reason is required for the audit trail."}
    return engine().capture_baseline(_get(namespace, name), reason)


@mcp.tool(annotations=READ_ONLY)
def arc_list_policies() -> dict[str, Any]:
    """The active rule catalogue (id, title, category, severity, enabled, rationale, fix), organisation settings,
    namespace overrides and the meaning of each posture."""
    p = engine().policy
    return {"rules": describe_rules(p.rules), "settings": p.settings, "overrides": p.overrides, "postures": POSTURE_HELP}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):  # noqa: ARG001
    from starlette.responses import PlainTextResponse

    return PlainTextResponse("ok")


def main() -> None:
    eng = engine()
    metrics.start(eng, int(os.environ.get("METRICS_PORT", "9090")), int(os.environ.get("SCAN_INTERVAL_SECONDS", "300")))
    mcp.run(
        transport="streamable-http",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()

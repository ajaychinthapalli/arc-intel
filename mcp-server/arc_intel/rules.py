"""Compliance rules for ARC AutoscalingRunnerSets (actions.github.com/v1alpha1).

Each rule is a pure function of (ars, settings, facts) so it can be unit-tested
without a cluster. `facts` carries the few things a rule can't see on the object
itself (secret key names, controller versions, NetworkPolicy count).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

RUNNER_CONTAINER = "runner"
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass
class Facts:
    """Cluster-side context gathered by the engine. None means 'unknown'."""

    secret_keys: list[str] | None = None      # key names only, never values
    secret_exists: bool | None = None
    controller_versions: list[str] | None = None
    network_policy_count: int | None = None


@dataclass
class Finding:
    rule_id: str
    title: str
    severity: str
    category: str
    message: str
    path: str = ""
    evidence: Any = None
    fix: str = ""
    origin: str = ""   # "drift" | "source_of_truth" once drift has been assessed

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "")}


@dataclass
class Rule:
    id: str
    title: str
    severity: str
    category: str
    rationale: str
    fix: str
    check: Callable[[dict, dict, Facts], Iterable[tuple[str, str, Any]]] = field(repr=False)


REGISTRY: dict[str, Rule] = {}


def rule(id: str, title: str, severity: str, category: str, rationale: str, fix: str):
    def deco(fn):
        REGISTRY[id] = Rule(id, title, severity, category, rationale, fix, fn)
        return fn

    return deco


# --------------------------------------------------------------------------- helpers

def spec(ars: dict) -> dict:
    return ars.get("spec") or {}


def pod_spec(ars: dict) -> dict:
    return ((spec(ars).get("template") or {}).get("spec")) or {}


def iter_containers(ars: dict, include_init: bool = True):
    ps = pod_spec(ars)
    kinds = ["containers", "initContainers"] if include_init else ["containers"]
    for kind in kinds:
        for c in ps.get(kind) or []:
            yield kind, c


def cpath(kind: str, c: dict, suffix: str = "") -> str:
    return f"spec.template.spec.{kind}[name={c.get('name', '?')}]" + (f".{suffix}" if suffix else "")


def runner_container(ars: dict) -> dict | None:
    cs = pod_spec(ars).get("containers") or []
    for c in cs:
        if c.get("name") == RUNNER_CONTAINER:
            return c
    return cs[0] if cs else None


def parse_image(image: str) -> tuple[str, str | None, str | None]:
    """Return (repository, tag, digest)."""
    digest = None
    if "@" in image:
        image, digest = image.split("@", 1)
    last = image.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = image.rsplit(":", 1)
        return repo, tag, digest
    return image, None, digest


def parse_version(s: str | None) -> tuple[int, int, int] | None:
    m = VERSION_RE.search(s or "")
    return tuple(int(x) for x in m.groups()) if m else None  # type: ignore[return-value]


def is_dind(ars: dict) -> bool:
    return any(c.get("name") == "dind" for _, c in iter_containers(ars))


def github_scope(url: str) -> str:
    """'enterprise', 'org' or 'repo' for a githubConfigUrl."""
    parts = [p for p in urlparse(url or "").path.split("/") if p]
    if parts[:1] == ["enterprises"]:
        return "enterprise"
    return "repo" if len(parts) >= 2 else "org"


# --------------------------------------------------------------------------- rules

@rule("ARC-001", "GitHub App authentication", "critical", "identity",
      "PATs are long-lived, user-bound and broadly scoped; GitHub App credentials are short-lived and org-owned.",
      "Create a secret with github_app_id, github_app_installation_id and github_app_private_key and set githubConfigSecret to it.")
def _github_app(ars, s, f):
    name = spec(ars).get("githubConfigSecret")
    path = "spec.githubConfigSecret"
    if not name:
        yield path, "githubConfigSecret is not set.", None
        return
    if f.secret_exists is False:
        yield path, f"Secret '{name}' referenced by githubConfigSecret does not exist; listener cannot authenticate.", name
        return
    if s.get("requireGithubApp") and f.secret_keys is not None:
        keys = set(f.secret_keys)
        if "github_token" in keys:
            yield path, f"Secret '{name}' uses a personal access token (github_token).", sorted(keys)
        elif not {"github_app_id", "github_app_private_key"} <= keys:
            yield path, f"Secret '{name}' is missing GitHub App keys.", sorted(keys)


@rule("ARC-002", "Allowed GitHub scope", "high", "identity",
      "Runners registered to an unexpected org/repo can execute untrusted workflows inside your cluster.",
      "Set githubConfigUrl to an approved org/repo URL.")
def _github_url(ars, s, f):
    url = spec(ars).get("githubConfigUrl") or ""
    allowed = s.get("allowedGithubConfigUrlPrefixes") or []
    if allowed and not any(url.rstrip("/") == p.rstrip("/") or url.startswith(p.rstrip("/") + "/") for p in allowed):
        yield "spec.githubConfigUrl", f"githubConfigUrl '{url}' is not in the approved list.", allowed


@rule("ARC-003", "Bounded maxRunners", "high", "cost",
      "An unbounded or very large scale set can exhaust cluster capacity and spend.",
      "Set maxRunners in the scale-set values to a value at or below the policy ceiling.")
def _max_runners(ars, s, f):
    mx = spec(ars).get("maxRunners")
    ceiling = s.get("maxRunnersCeiling")
    if mx is None:
        yield "spec.maxRunners", "maxRunners is unset (unbounded scaling).", None
    elif ceiling is not None and mx > ceiling:
        yield "spec.maxRunners", f"maxRunners={mx} exceeds the ceiling of {ceiling}.", mx


@rule("ARC-004", "Sane minRunners", "medium", "cost",
      "Warm runners are billed while idle; minRunners above maxRunners is invalid.",
      "Lower minRunners (0 is fine for most workloads).")
def _min_runners(ars, s, f):
    sp = spec(ars)
    mn, mx = sp.get("minRunners"), sp.get("maxRunners")
    if mn is None:
        return
    if mx is not None and mn > mx:
        yield "spec.minRunners", f"minRunners={mn} is greater than maxRunners={mx}.", mn
    elif mn > s.get("maxMinRunners", 5):
        yield "spec.minRunners", f"minRunners={mn} keeps more than {s.get('maxMinRunners')} idle runners warm.", mn


@rule("ARC-005", "Pinned images", "high", "supply-chain",
      "':latest' or untagged images change underneath you and make builds unreproducible.",
      "Pin images to an explicit version tag, ideally with an @sha256 digest.")
def _pinned(ars, s, f):
    for kind, c in iter_containers(ars):
        img = c.get("image") or ""
        _, tag, digest = parse_image(img)
        if not digest and tag in (None, "latest"):
            yield cpath(kind, c, "image"), f"Image '{img}' is not pinned.", img


@rule("ARC-006", "Approved registries", "high", "supply-chain",
      "Images from unapproved registries bypass your scanning and provenance controls.",
      "Mirror the image into an approved registry or add the registry to allowedRegistries.")
def _registries(ars, s, f):
    allowed = s.get("allowedRegistries") or []
    for kind, c in iter_containers(ars):
        img = c.get("image") or ""
        if allowed and not any(img.startswith(p) for p in allowed):
            yield cpath(kind, c, "image"), f"Image '{img}' is not from an approved registry.", img


@rule("ARC-007", "Minimum runner version", "medium", "lifecycle",
      "GitHub deprecates old runner versions; outdated runners stop picking up jobs.",
      "Bump the runner image tag to at least the policy minimum.")
def _runner_version(ars, s, f):
    c = runner_container(ars)
    if not c:
        return
    _, tag, _ = parse_image(c.get("image") or "")
    have, want = parse_version(tag), parse_version(s.get("minRunnerVersion"))
    if have and want and have < want:
        yield cpath("containers", c, "image"), f"Runner {'.'.join(map(str, have))} is older than {s.get('minRunnerVersion')}.", tag


@rule("ARC-008", "No privileged containers", "critical", "runtime-security",
      "A privileged container is root on the node; a malicious workflow can escape to the host.",
      "Use containerMode 'kubernetes' or rootless builders (BuildKit/Kaniko); allow DinD only in exempted namespaces.")
def _privileged(ars, s, f):
    for kind, c in iter_containers(ars):
        sc = c.get("securityContext") or {}
        if sc.get("privileged"):
            if c.get("name") == "dind" and s.get("allowDind"):
                continue
            yield cpath(kind, c, "securityContext.privileged"), f"Container '{c.get('name')}' runs privileged.", True


@rule("ARC-009", "No host namespaces or hostPath", "high", "runtime-security",
      "Host networking/PID/IPC and hostPath mounts break pod isolation.",
      "Remove hostNetwork/hostPID/hostIPC and hostPath volumes from the runner template.")
def _host_access(ars, s, f):
    ps = pod_spec(ars)
    for k in ("hostNetwork", "hostPID", "hostIPC"):
        if ps.get(k):
            yield f"spec.template.spec.{k}", f"{k} is enabled.", True
    for v in ps.get("volumes") or []:
        if "hostPath" in v:
            yield f"spec.template.spec.volumes[name={v.get('name')}].hostPath", f"Volume '{v.get('name')}' mounts a host path.", v["hostPath"]


@rule("ARC-010", "Runner resource requests and limits", "medium", "reliability",
      "Without requests/limits, runners are scheduled blindly and noisy builds starve neighbours.",
      "Set cpu/memory requests and limits on the runner container.")
def _resources(ars, s, f):
    c = runner_container(ars)
    if not c:
        return
    res = c.get("resources") or {}
    missing = [f"{part}.{r}" for part in ("requests", "limits") for r in ("cpu", "memory") if r not in (res.get(part) or {})]
    if missing:
        yield cpath("containers", c, "resources"), f"Runner container is missing {', '.join(missing)}.", res or None


@rule("ARC-011", "No privilege escalation in runner", "medium", "runtime-security",
      "allowPrivilegeEscalation lets setuid binaries gain more privileges than the process started with.",
      "Set securityContext.allowPrivilegeEscalation: false on the runner container.")
def _priv_esc(ars, s, f):
    c = runner_container(ars)
    if c and (c.get("securityContext") or {}).get("allowPrivilegeEscalation") is not False:
        yield cpath("containers", c, "securityContext.allowPrivilegeEscalation"), "allowPrivilegeEscalation is not explicitly false.", None


@rule("ARC-012", "Explicit runner group", "low", "governance",
      "Org/enterprise runners in the Default group are available to every repository.",
      "Set runnerGroup to a group restricted to the repositories that need it.")
def _runner_group(ars, s, f):
    sp = spec(ars)
    if not s.get("forbidDefaultRunnerGroup"):
        return
    if github_scope(sp.get("githubConfigUrl", "")) != "repo" and (sp.get("runnerGroup") or "default").lower() == "default":
        yield "spec.runnerGroup", "Org/enterprise scale set is in the Default runner group.", sp.get("runnerGroup")


@rule("ARC-013", "Required ownership labels", "low", "governance",
      "Owner and cost-center labels are needed for chargeback and incident routing.",
      "Add the labels via the scale-set chart's labels/annotations values.")
def _labels(ars, s, f):
    labels = (ars.get("metadata") or {}).get("labels") or {}
    missing = [k for k in s.get("requiredLabels") or [] if not labels.get(k)]
    if missing:
        yield "metadata.labels", f"Missing required labels: {', '.join(missing)}.", missing


@rule("ARC-014", "NetworkPolicy present", "medium", "runtime-security",
      "Runners execute arbitrary code; without egress policy they can reach internal services.",
      "Add a default-deny NetworkPolicy in the runner namespace with explicit egress to GitHub and registries.")
def _netpol(ars, s, f):
    if s.get("requireNetworkPolicy") and f.network_policy_count == 0:
        ns = (ars.get("metadata") or {}).get("namespace")
        yield "metadata.namespace", f"Namespace '{ns}' has no NetworkPolicy.", 0


@rule("ARC-015", "Controller / scale-set version match", "high", "lifecycle",
      "ARC requires the gha-runner-scale-set chart and controller to run the same version.",
      "Upgrade the scale set (or controller) so both charts use the same version.")
def _version_skew(ars, s, f):
    if f.controller_versions is None:
        return
    if not f.controller_versions:
        yield "", "No gha-runner-scale-set-controller deployment found in the cluster.", None
        return
    v = ((ars.get("metadata") or {}).get("labels") or {}).get("app.kubernetes.io/version")
    if v and v not in f.controller_versions:
        yield "metadata.labels.app.kubernetes.io/version", f"Scale set is version {v}; controller is {', '.join(f.controller_versions)}.", v


@rule("ARC-016", "Declaratively managed", "medium", "governance",
      "Scale sets created by hand have no source of truth, so drift can't be detected or reverted.",
      "Manage the scale set with Helm, Argo CD or Flux.")
def _managed(ars, s, f):
    meta = ars.get("metadata") or {}
    labels, ann = meta.get("labels") or {}, meta.get("annotations") or {}
    managed = (
        labels.get("app.kubernetes.io/managed-by", "").lower() == "helm"
        or any(k.startswith("argocd.argoproj.io") for k in list(labels) + list(ann))
        or any(k.startswith(("kustomize.toolkit.fluxcd.io", "helm.toolkit.fluxcd.io")) for k in labels)
    )
    if not managed:
        yield "metadata.labels", "No Helm/Argo CD/Flux ownership markers found.", None


# --------------------------------------------------------------------------- evaluation

def evaluate(ars: dict, settings: dict, facts: Facts, rule_overrides: dict | None = None) -> list[Finding]:
    rule_overrides = rule_overrides or {}
    out: list[Finding] = []
    for r in REGISTRY.values():
        cfg = rule_overrides.get(r.id) or {}
        if cfg.get("enabled") is False:
            continue
        sev = cfg.get("severity", r.severity)
        for path, msg, evidence in r.check(ars, settings, facts):
            out.append(Finding(r.id, r.title, sev, r.category, msg, path, evidence, r.fix))
    return out


def describe_rules(rule_overrides: dict | None = None) -> list[dict]:
    rule_overrides = rule_overrides or {}
    rows = []
    for r in REGISTRY.values():
        cfg = rule_overrides.get(r.id) or {}
        rows.append({
            "id": r.id, "title": r.title, "category": r.category,
            "severity": cfg.get("severity", r.severity), "enabled": cfg.get("enabled", True),
            "rationale": r.rationale, "fix": r.fix,
        })
    return rows

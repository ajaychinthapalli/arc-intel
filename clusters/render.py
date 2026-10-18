"""Render clusters/_template into clusters/<cluster>/ from live cluster data. Stdlib only.

Called by demo/scripts/adopt-existing-eks.sh (phase `generate`), which passes a JSON context:
  {cluster, region, repo_url, repo_revision, image_repo, image_tag, github_config_url,
   github_secret_name, model_config, agent_sa, agent_sa_create, versions{arc, eso, kps},
   controller{release, namespace, values}, runner_namespace, scale_sets[{release, values}]}

Helm values exported from the cluster are written to Git, so anything that looks like a credential
stops the render instead of being committed.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SECRET_KEY = re.compile(r"(token|password|passwd|private[_-]?key|secret[_-]?key|api[_-]?key|credential)", re.I)


# --------------------------------------------------------------------------- minimal YAML emitter

def _scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    s = str(v)
    plain = re.fullmatch(r"[A-Za-z0-9_./@:+-][A-Za-z0-9_./@:+ -]*", s) and not re.fullmatch(
        r"(true|false|yes|no|on|off|null|~|[-+]?\d[\d_.eE+-]*)", s, re.I) and not s.endswith(":") and ": " not in s
    return s if plain else json.dumps(s)


def to_yaml(obj, indent: int = 0) -> str:
    pad = "  " * indent
    out: list[str] = []
    if isinstance(obj, dict):
        if not obj:
            return pad + "{}\n"
        for k, v in obj.items():
            key = _scalar(k)
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{key}:\n{to_yaml(v, indent + 1)}")
            else:
                out.append(f"{pad}{key}: {_scalar(v) if not isinstance(v, (dict, list)) else ('{}' if isinstance(v, dict) else '[]')}\n")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)) and item:
                body = to_yaml(item, indent + 1)
                first, _, rest = body.partition("\n")
                out.append(f"{pad}- {first.lstrip()}\n{rest}")
            else:
                out.append(f"{pad}- {_scalar(item) if not isinstance(item, (dict, list)) else ('{}' if isinstance(item, dict) else '[]')}\n")
    else:
        out.append(pad + _scalar(obj) + "\n")
    return "".join(out)


# --------------------------------------------------------------------------- credential checks

def find_credentials(obj, path="") -> list[str]:
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if SECRET_KEY.search(str(k)) and isinstance(v, str) and v and not str(k).lower().endswith(("name", "ref", "secretname")):
                hits.append(p)
            hits += find_credentials(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += find_credentials(v, f"{path}[{i}]")
    return hits


def sanitize_scale_set(values: dict, github_secret_name: str) -> tuple[dict, list[str]]:
    notes = []
    values = dict(values or {})
    gcs = values.get("githubConfigSecret")
    if isinstance(gcs, dict):
        values["githubConfigSecret"] = github_secret_name
        notes.append(f"githubConfigSecret: inline credentials replaced by Secret '{github_secret_name}' "
                     "(synced from Secrets Manager arc/github-app; same Secret the scale set already uses)")
    elif gcs != github_secret_name:
        values["githubConfigSecret"] = github_secret_name
    return values, notes


# --------------------------------------------------------------------------- render

def render(ctx: dict, force: bool = False) -> Path:
    out = ROOT / ctx["cluster"]
    if out.exists() and not force:
        sys.exit(f"{out} already exists; set FORCE=1 to overwrite.")
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "_template", out)

    problems, notes = [], []

    # Helm values -> Git
    ctrl_vals = ctx["controller"]["values"] or {}
    problems += [f"controller values: {p}" for p in find_credentials(ctrl_vals)]
    (out / "values").mkdir(exist_ok=True)
    (out / "values" / "arc-controller.yaml").write_text(
        f"# Values of Helm release '{ctx['controller']['release']}' exported from the cluster (helm get values).\n"
        + (to_yaml(ctrl_vals) if ctrl_vals else "{}\n"))
    for ss in ctx["scale_sets"]:
        vals, n = sanitize_scale_set(ss["values"], ctx["github_secret_name"])
        notes += [f"{ss['release']}: {x}" for x in n]
        problems += [f"{ss['release']} values: {p}" for p in find_credentials(vals)]
        d = out / "runner-scale-sets" / ss["release"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "values.yaml").write_text(
            f"# Values of Helm release '{ss['release']}' exported from the cluster (helm get values).\n"
            "# Change runner pools through PRs to this file; Argo CD applies them.\n" + to_yaml(vals))
    if problems:
        shutil.rmtree(out)
        sys.exit("Refusing to write credentials to Git. Move these to a Kubernetes Secret first:\n  - "
                 + "\n  - ".join(problems))

    # Agent ServiceAccount
    agent_dir = out / "manifests" / "agent"
    if ctx["agent_sa_create"]:
        (agent_dir / "serviceaccount.yaml").write_text(
            "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n"
            f"  name: {ctx['agent_sa']}\n  namespace: kagent\n"
            "  # Bedrock credentials come from an EKS Pod Identity association on this ServiceAccount.\n")
        sa_res = "  - serviceaccount.yaml"
    else:
        sa_res = ""

    subs = {
        "__CLUSTER__": ctx["cluster"], "__REGION__": ctx["region"],
        "__REPO_URL__": ctx["repo_url"], "__REPO_REVISION__": ctx["repo_revision"],
        "__IMAGE_REPO__": ctx["image_repo"], "__IMAGE_TAG__": ctx["image_tag"],
        "__GITHUB_CONFIG_URL__": ctx["github_config_url"], "__GITHUB_SECRET_NAME__": ctx["github_secret_name"],
        "__MODEL_CONFIG__": ctx["model_config"], "__AGENT_SA__": ctx["agent_sa"],
        "__AGENT_SA_RESOURCE__": sa_res,
        "__ARC_VERSION__": ctx["versions"]["arc"], "__ESO_VERSION__": ctx["versions"]["eso"],
        "__KPS_VERSION__": ctx["versions"]["kps"],
        "__ARC_CONTROLLER_RELEASE__": ctx["controller"]["release"],
        "__ARC_CONTROLLER_NAMESPACE__": ctx["controller"]["namespace"],
        "__RUNNER_NAMESPACE__": ctx["runner_namespace"],
    }
    for f in out.rglob("*.yaml"):
        s = f.read_text()
        for k, v in subs.items():
            s = s.replace(k, v)
        s = re.sub(r"\n\n+patches:", "\npatches:", s)  # tidy when no SA resource line
        f.write_text(s)
    left = sorted({m for f in out.rglob("*.yaml") for m in re.findall(r"__[A-Z_]+__", f.read_text())})
    if left:
        sys.exit(f"Unreplaced placeholders: {left}")

    (out / "ADOPTION-NOTES.md").write_text(
        f"# Adoption notes for {ctx['cluster']}\n\n"
        f"- ARC chart version: {ctx['versions']['arc']} (controller release `{ctx['controller']['release']}` in "
        f"`{ctx['controller']['namespace']}`)\n"
        f"- Scale sets adopted: {', '.join(s['release'] for s in ctx['scale_sets'])} (namespace `{ctx['runner_namespace']}`)\n"
        f"- GitHub config Secret: `{ctx['github_secret_name']}` (kept; External Secrets merges arc/github-app into it)\n"
        f"- Agent: ModelConfig `{ctx['model_config']}`, ServiceAccount `{ctx['agent_sa']}`\n"
        + "".join(f"- {n}\n" for n in notes))
    return out


if __name__ == "__main__":
    ctx = json.load(open(sys.argv[1]))
    path = render(ctx, force="--force" in sys.argv)
    print(path)

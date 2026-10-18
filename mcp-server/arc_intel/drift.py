"""Drift detection, change attribution and remediation-patch generation.

Desired state comes from (in order): a captured baseline, the deployed Helm
release manifest, or the kubectl last-applied annotation. The diff is
structural and name-aware for Kubernetes lists (containers, env, volumes...),
so reordering doesn't register as drift but a changed image does.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

# ----------------------------------------------------------------------------- model


@dataclass
class DriftEntry:
    path: str                 # human path, e.g. spec.template.spec.containers[name=runner].image
    op: str                   # changed | added (only live) | removed (only desired)
    desired: Any
    live: Any
    pointer: str              # RFC 6901 pointer into the *live* object
    severity: str = "low"
    changed_by: list[dict] | None = None
    attribution: str = ""     # exact | parent | unknown
    manual_change: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", [])}


# Paths are matched against the human path. First match wins.
SEVERITY_RULES: list[tuple[str, re.Pattern]] = [
    ("critical", re.compile(r"(securityContext\.privileged|hostNetwork|hostPID|hostIPC|hostPath|"
                            r"^spec\.githubConfigSecret|^spec\.githubConfigUrl|serviceAccountName)")),
    ("high", re.compile(r"(\.image$|securityContext|^spec\.runnerGroup|\.command|\.args|\.env\b|"
                        r"^spec\.proxy|^spec\.githubServerTLS|volumes)")),
    ("medium", re.compile(r"(^spec\.(max|min)Runners|resources|nodeSelector|tolerations|affinity|"
                          r"listenerTemplate|^spec\.runnerScaleSetName)")),
]

# Fields the ARC controller or API server manage; never drift.
IGNORE = [
    re.compile(r"^spec\.template\.metadata\.creationTimestamp$"),
]


def classify(path: str) -> str:
    for sev, rx in SEVERITY_RULES:
        if rx.search(path):
            return sev
    return "low"


# ----------------------------------------------------------------------------- diff

def _empty(v: Any) -> bool:
    return v is None or v == {} or v == [] or v == ""


def _esc(k: str) -> str:
    return str(k).replace("~", "~0").replace("/", "~1")


def _keyed(lst: list) -> bool:
    return bool(lst) and all(isinstance(x, dict) and "name" in x for x in lst)


def _join(segs: list[str]) -> str:
    out = ""
    for s in segs:
        out += s if s.startswith("[") else (("." if out else "") + s)
    return out


def _diff(d: Any, l: Any, segs: list[str], ptr: str, out: list[DriftEntry]) -> None:
    if isinstance(d, dict) and isinstance(l, dict):
        for k in sorted(set(d) | set(l), key=str):
            dv, lv = d.get(k), l.get(k)
            if _empty(dv) and _empty(lv):
                continue
            child, cptr = segs + [str(k)], f"{ptr}/{_esc(k)}"
            if _empty(lv):
                out.append(DriftEntry(_join(child), "removed", dv, None, cptr))
            elif _empty(dv):
                out.append(DriftEntry(_join(child), "added", None, lv, cptr))
            else:
                _diff(dv, lv, child, cptr, out)
    elif isinstance(d, list) and isinstance(l, list) and _keyed(d) and _keyed(l):
        lidx = {x["name"]: i for i, x in enumerate(l)}
        dmap = {x["name"]: x for x in d}
        for name, dx in dmap.items():
            seg = segs + [f"[name={name}]"]
            if name in lidx:
                _diff(dx, l[lidx[name]], seg, f"{ptr}/{lidx[name]}", out)
            else:
                out.append(DriftEntry(_join(seg), "removed", dx, None, f"{ptr}/-"))
        for name, i in lidx.items():
            if name not in dmap:
                out.append(DriftEntry(_join(segs + [f"[name={name}]"]), "added", None, l[i], f"{ptr}/{i}"))
    elif d != l:
        out.append(DriftEntry(_join(segs), "changed", d, l, ptr))


def diff_ars(desired: dict, live: dict) -> list[DriftEntry]:
    """Diff spec fully; for metadata labels/annotations only report keys the desired state sets."""
    out: list[DriftEntry] = []
    _diff(desired.get("spec") or {}, live.get("spec") or {}, ["spec"], "/spec", out)
    for section in ("labels", "annotations"):
        want = ((desired.get("metadata") or {}).get(section)) or {}
        have = ((live.get("metadata") or {}).get(section)) or {}
        for k, v in want.items():
            if have.get(k) != v:
                op = "removed" if k not in have else "changed"
                out.append(DriftEntry(f"metadata.{section}.{k}", op, v, have.get(k), f"/metadata/{section}/{_esc(k)}"))
    out = [e for e in out if not any(rx.search(e.path) for rx in IGNORE)]
    for e in out:
        e.severity = classify(e.path)
    return out


# ----------------------------------------------------------------------------- attribution

_SEG_RE = re.compile(r"\[name=([^\]]+)\]|([^.\[]+)")


def _field_keys(path: str) -> list[str]:
    keys: list[str] = []
    if path.startswith("metadata.labels.") or path.startswith("metadata.annotations."):
        section, key = path.split(".", 2)[1:]
        return ["f:metadata", f"f:{section}", f"f:{key}"]
    for m in _SEG_RE.finditer(path):
        if m.group(1) is not None:
            keys.append("k:" + json.dumps({"name": m.group(1)}, separators=(",", ":")))
        else:
            keys.append("f:" + m.group(2))
    return keys


def attribute(entries: list[DriftEntry], managed_fields: list[dict], expected_managers: list[str]) -> None:
    """Annotate each entry with the field manager(s) that own the drifted path.

    Server-side field tracking records who last set each field. For changed /
    added fields the exact owner is known; for removed fields only the owner of
    the parent can be inferred.
    """
    mfs = [m for m in managed_fields or [] if not m.get("subresource")]
    expected = {m.lower() for m in expected_managers}
    for e in entries:
        keys = _field_keys(e.path)
        best, owners = 0, []
        for mf in mfs:
            node, depth = mf.get("fieldsV1") or {}, 0
            for k in keys:
                if isinstance(node, dict) and k in node:
                    node, depth = node[k], depth + 1
                else:
                    break
            if depth and depth > best:
                best, owners = depth, [mf]
            elif depth and depth == best:
                owners.append(mf)
        if not owners:
            e.attribution = "unknown"
            continue
        # Most recent writer first.
        owners.sort(key=lambda m: m.get("time") or "", reverse=True)
        e.changed_by = [{"manager": m.get("manager"), "operation": m.get("operation"), "time": m.get("time")} for m in owners]
        e.attribution = "exact" if best == len(keys) and e.op != "removed" else "parent"
        e.manual_change = (owners[0].get("manager") or "").lower() not in expected


# ----------------------------------------------------------------------------- remediation

def _ptr_sort_key(ptr: str):
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in ptr.strip("/").split("/")]


def remediation_patch(entries: list[DriftEntry]) -> list[dict]:
    """RFC 6902 JSON patch that restores desired values.

    `test` ops guard every replace/remove so the patch fails (instead of
    clobbering) if someone changes the field again before it's applied.
    Removes run in descending index order so earlier indices stay valid.
    """
    tests, replaces, removes, adds = [], [], [], []
    for e in entries:
        if e.op == "changed":
            tests.append({"op": "test", "path": e.pointer, "value": e.live})
            replaces.append({"op": "replace", "path": e.pointer, "value": e.desired})
        elif e.op == "added":
            tests.append({"op": "test", "path": e.pointer, "value": e.live})
            removes.append({"op": "remove", "path": e.pointer})
        elif e.op == "removed":
            adds.append({"op": "add", "path": e.pointer, "value": e.desired})
    removes.sort(key=lambda o: _ptr_sort_key(o["path"]), reverse=True)
    return tests + replaces + removes + adds

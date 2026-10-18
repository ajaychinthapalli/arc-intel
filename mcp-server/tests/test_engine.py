"""Run with `python -m pytest` or `python -m unittest discover tests`."""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arc_intel import drift as D  # noqa: E402
from arc_intel import rules as R  # noqa: E402
from arc_intel.cluster import StaticCluster, ars_from_manifest, decode_helm_release, encode_helm_release  # noqa: E402
from arc_intel.engine import Engine  # noqa: E402
from arc_intel.policy import Policy  # noqa: E402
from tests import fixtures  # noqa: E402

POLICY = os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "mcp-server", "policy.yaml")


class _Cluster(StaticCluster):
    def __init__(self):  # build in-memory from fixtures instead of reading files
        self.objs = fixtures.build()
        self.baselines = {}
        for o in self._kind("ConfigMap"):
            self.baselines.update({k: json.loads(v) for k, v in o["data"].items()})


def engine():
    return Engine(_Cluster(), Policy.load(POLICY))


def ids(findings):
    return {f["rule_id"] if isinstance(f, dict) else f.rule_id for f in findings}


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.e = engine()

    def test_compliant_scale_set_has_no_findings(self):
        ars = self.e.cluster.get_ars("arc-runners", "linux-standard")
        self.assertEqual(self.e.evaluate(ars), [])

    def test_legacy_scale_set_hits_expected_rules(self):
        ars = self.e.cluster.get_ars("arc-runners-legacy", "legacy-build")
        got = ids(self.e.evaluate(ars))
        for rid in ["ARC-001", "ARC-002", "ARC-003", "ARC-004", "ARC-006", "ARC-007", "ARC-008",
                    "ARC-009", "ARC-010", "ARC-011", "ARC-012", "ARC-013", "ARC-014", "ARC-015", "ARC-016"]:
            self.assertIn(rid, got, rid)

    def test_dind_allowed_only_by_override(self):
        ars = self.e.cluster.get_ars("arc-runners-docker", "docker-builds")
        self.assertNotIn("ARC-008", ids(self.e.evaluate(ars)))
        moved = copy.deepcopy(ars)
        moved["metadata"]["namespace"] = "arc-runners"
        self.assertIn("ARC-008", ids(R.evaluate(moved, Policy.load(POLICY).settings_for(moved), R.Facts())))

    def test_rule_can_be_disabled_and_regraded(self):
        p = Policy.from_dict({"rules": {"ARC-013": {"enabled": False}, "ARC-003": {"severity": "critical"}}})
        ars = self.e.cluster.get_ars("arc-runners-legacy", "legacy-build")
        out = R.evaluate(ars, p.settings_for(ars), R.Facts(), p.rules)
        self.assertNotIn("ARC-013", ids(out))
        self.assertEqual(next(f.severity for f in out if f.rule_id == "ARC-003"), "critical")

    def test_image_parsing(self):
        self.assertEqual(R.parse_image("ghcr.io/a/b:1.2@sha256:x"), ("ghcr.io/a/b", "1.2", "sha256:x"))
        self.assertEqual(R.parse_image("localhost:5000/runner"), ("localhost:5000/runner", None, None))
        self.assertEqual(R.github_scope("https://github.com/org/repo"), "repo")
        self.assertEqual(R.github_scope("https://github.com/enterprises/acme"), "enterprise")


class DriftTests(unittest.TestCase):
    def test_list_reorder_is_not_drift(self):
        a = {"spec": {"template": {"spec": {"containers": [{"name": "a", "image": "x"}, {"name": "b", "image": "y"}]}}}}
        b = copy.deepcopy(a)
        b["spec"]["template"]["spec"]["containers"].reverse()
        self.assertEqual(D.diff_ars(a, b), [])

    def test_changed_added_removed(self):
        desired = {"spec": {"maxRunners": 10, "template": {"spec": {"containers": [
            {"name": "runner", "env": [{"name": "A", "value": "1"}, {"name": "B", "value": "2"}]}]}}}}
        live = copy.deepcopy(desired)
        live["spec"]["maxRunners"] = 99
        env = live["spec"]["template"]["spec"]["containers"][0]["env"]
        env.pop(1)
        env.append({"name": "C", "value": "3"})
        got = {(e.path, e.op) for e in D.diff_ars(desired, live)}
        self.assertEqual(got, {
            ("spec.maxRunners", "changed"),
            ("spec.template.spec.containers[name=runner].env[name=B]", "removed"),
            ("spec.template.spec.containers[name=runner].env[name=C]", "added"),
        })

    def test_severity_classification(self):
        self.assertEqual(D.classify("spec.template.spec.containers[name=dind].securityContext.privileged"), "critical")
        self.assertEqual(D.classify("spec.template.spec.containers[name=runner].image"), "high")
        self.assertEqual(D.classify("spec.maxRunners"), "medium")
        self.assertEqual(D.classify("metadata.labels.owner"), "low")

    def test_patch_reverts_drift(self):
        e = engine()
        live = e.cluster.get_ars("arc-runners", "linux-large")
        d = e.drift(live)
        patch = D.remediation_patch(d["_entries"])
        self.assertTrue(all(op["op"] in ("test", "replace", "remove", "add") for op in patch))
        reverted = _apply(copy.deepcopy(live), patch)
        self.assertEqual(D.diff_ars(d["_desired"], reverted), [])

    def test_patch_test_ops_guard_against_concurrent_change(self):
        e = engine()
        live = e.cluster.get_ars("arc-runners", "linux-large")
        patch = D.remediation_patch(e.drift(live)["_entries"])
        live["spec"]["maxRunners"] = 500  # changed again before the patch lands
        with self.assertRaises(AssertionError):
            _apply(live, patch)


class AttributionTests(unittest.TestCase):
    def test_manual_changes_are_attributed(self):
        d = engine().drift(engine().cluster.get_ars("arc-runners", "linux-large"))
        by_path = {e["path"]: e for e in d["entries"]}
        self.assertEqual(by_path["spec.maxRunners"]["changed_by"][0]["manager"], "kubectl-edit")
        self.assertEqual(by_path["spec.template.spec.containers[name=runner].image"]["changed_by"][0]["manager"], "kubectl-patch")
        self.assertTrue(all(e["manual_change"] for e in d["entries"]))

    def test_list_item_key_attribution(self):
        d = engine().drift(engine().cluster.get_ars("arc-runners-docker", "docker-builds"))
        (entry,) = d["entries"]
        self.assertEqual(entry["path"], "spec.template.spec.containers[name=runner].env[name=RUNNER_DEBUG]")
        self.assertEqual(entry["changed_by"][0]["manager"], "kubectl-edit")
        self.assertEqual(entry["attribution"], "exact")


class PostureTests(unittest.TestCase):
    def test_four_postures(self):
        e = engine()
        got = {f"{a['namespace']}/{a['name']}": a["posture"] for a in map(e.assess, e.cluster.list_ars())}
        self.assertEqual(got, {
            "arc-runners/linux-standard": "healthy",
            "arc-runners/linux-large": "drift_introduced_violation",
            "arc-runners-docker/docker-builds": "drifted",
            "arc-runners-legacy/legacy-build": "non_compliant",
            "arc-runners/arm64-builds": "unknown_baseline",
        })

    def test_violation_origin(self):
        e = engine()
        a = e.assess(e.cluster.get_ars("arc-runners", "linux-large"))
        self.assertEqual({v["rule_id"]: v["origin"] for v in a["violations"]}, {"ARC-003": "drift", "ARC-005": "drift"})

    def test_fleet_report(self):
        r = engine().fleet_report()
        self.assertEqual(r["scale_sets"], 5)
        self.assertEqual(r["scale_set_summaries"][0]["ref"], "arc-runners-legacy/legacy-build")
        self.assertEqual(r["top_risks"][0]["severity"], "critical")

    def test_capture_baseline_then_no_drift(self):
        e = engine()
        live = e.cluster.get_ars("arc-runners-docker", "docker-builds")
        e.capture_baseline(live, "approve debug flag")
        self.assertFalse(e.drift(live)["drifted"])


class HelmTests(unittest.TestCase):
    def test_roundtrip_and_secret_never_extracted(self):
        rel = {"manifest": "kind: Secret\nmetadata: {name: s}\nstringData: {github_token: x}\n---\n"
                           "kind: AutoscalingRunnerSet\nmetadata: {name: a}\nspec: {maxRunners: 3}\n"}
        decoded = decode_helm_release(encode_helm_release(rel))
        doc = ars_from_manifest(decoded["manifest"], "a")
        self.assertEqual(doc["spec"]["maxRunners"], 3)
        self.assertNotIn("github_token", json.dumps(doc))


def _apply(doc, patch):
    """Minimal RFC 6902 applier for tests."""
    def walk(ptr):
        parts = [p.replace("~1", "/").replace("~0", "~") for p in ptr.strip("/").split("/")]
        node = doc
        for p in parts[:-1]:
            node = node[int(p)] if isinstance(node, list) else node[p]
        return node, parts[-1]

    for op in patch:
        parent, key = walk(op["path"])
        idx = (len(parent) if key == "-" else int(key)) if isinstance(parent, list) else key
        if op["op"] == "test":
            assert parent[idx] == op["value"], f"test failed at {op['path']}"
        elif op["op"] == "replace":
            parent[idx] = op["value"]
        elif op["op"] == "remove":
            del parent[idx]
        elif op["op"] == "add":
            parent.insert(idx, op["value"]) if isinstance(parent, list) else parent.__setitem__(idx, op["value"])
    return doc


if __name__ == "__main__":
    unittest.main()

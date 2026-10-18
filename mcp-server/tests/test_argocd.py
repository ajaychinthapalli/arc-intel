import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arc_intel.argocd import ArgoCDClient, StaticArgoCD, owning_application  # noqa: E402
from arc_intel.audit import CloudWatchAudit  # noqa: E402
from arc_intel.engine import Engine  # noqa: E402
from arc_intel.policy import Policy  # noqa: E402
from tests.test_audit import StubLogs, _row  # noqa: E402
from tests.test_engine import POLICY, _Cluster  # noqa: E402


def engine(**kw):
    c = _Cluster()
    return Engine(c, Policy.load(POLICY), argocd=StaticArgoCD(c.objs), **kw)


class TrackingTests(unittest.TestCase):
    def test_annotation_tracking(self):
        ars = {"metadata": {"annotations": {
            "argocd.argoproj.io/tracking-id": "runners:actions.github.com/AutoscalingRunnerSet:ns/x"}}}
        self.assertEqual(owning_application(ars), ("runners", "argocd"))

    def test_apps_in_any_namespace(self):
        ars = {"metadata": {"annotations": {
            "argocd.argoproj.io/tracking-id": "team-a_runners:actions.github.com/AutoscalingRunnerSet:ns/x"}}}
        self.assertEqual(owning_application(ars), ("runners", "team-a"))

    def test_helm_instance_label_is_not_argo(self):
        ars = {"metadata": {"labels": {"app.kubernetes.io/instance": "x", "app.kubernetes.io/managed-by": "Helm"}}}
        self.assertIsNone(owning_application(ars))


class ArgoDriftTests(unittest.TestCase):
    def test_argo_target_is_source_of_truth(self):
        e = engine()
        a = e.assess(e.cluster.get_ars("arc-runners", "arm64-builds"))
        self.assertEqual(a["posture"], "drifted")
        d = a["drift"]
        self.assertEqual(d["baseline_source"], "argocd:arc-runners-arm64")
        (entry,) = d["entries"]
        self.assertEqual(entry["path"], "spec.template.spec.containers[name=runner].resources.limits.cpu")
        self.assertEqual(entry["changed_by"][0]["manager"], "kubectl-edit")
        g = d["gitops"]
        self.assertEqual((g["application"], g["sync_status"], g["resource_status"], g["self_heal"]),
                         ("arc-runners-arm64", "OutOfSync", "OutOfSync", False))
        self.assertEqual(g["source"]["path"], "runner-scale-sets/arm64-builds")

    def test_remediation_routes_to_argo(self):
        e = engine()
        r = e.remediation(e.cluster.get_ars("arc-runners", "arm64-builds"))
        self.assertIn("argocd app sync arc-runners-arm64", r["drift_revert"]["guidance"])

    def test_self_healed_change_is_still_reported(self):
        e = engine()
        ars = e.cluster.get_ars("arc-runners", "arm64-builds")
        ars["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "2"  # Argo reverted it
        app = e.cluster.argocd_application("argocd", "arc-runners-arm64")
        app["spec"]["syncPolicy"]["automated"]["selfHeal"] = True
        stub = StubLogs([
            _row("2026-10-17 18:20:31.000", "arn:aws:sts::111122223333:assumed-role/Dev/bob",
                 "arn:aws:iam::111122223333:role/Dev", "bob", verb="update"),
            _row("2026-10-17 18:21:02.000", "system:serviceaccount:argocd:argocd-application-controller", None, None),
        ])
        e.audit = CloudWatchAudit("/aws/eks/c/cluster", client=stub, poll_seconds=0)
        d = e.drift(ars, with_audit=True)
        self.assertFalse(d["drifted"])
        self.assertTrue(d["self_healed"])
        self.assertEqual([w["session"] for w in d["out_of_band_writes"]], ["bob"])


class FakeArgo(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        FakeArgo.seen.append((u.path, parse_qs(u.query), self.headers.get("Authorization")))
        target = {"kind": "AutoscalingRunnerSet", "metadata": {"name": "x", "namespace": "ns"}, "spec": {"maxRunners": 7}}
        body = {"items": [{"kind": "AutoscalingRunnerSet", "namespace": "ns", "name": "x",
                           "targetState": json.dumps(target), "liveState": "{}"}]}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class ArgoClientTests(unittest.TestCase):
    def test_managed_resources_call(self):
        srv = HTTPServer(("127.0.0.1", 0), FakeArgo)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = ArgoCDClient(f"http://127.0.0.1:{srv.server_port}", "tok")
            t = c.target_state("runners", "argocd", "ns", "x")
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(t["spec"]["maxRunners"], 7)
        path, q, auth = FakeArgo.seen[-1]
        self.assertEqual(path, "/api/v1/applications/runners/managed-resources")
        self.assertEqual((q["kind"], q["name"], q["appNamespace"]), (["AutoscalingRunnerSet"], ["x"], ["argocd"]))
        self.assertEqual(auth, "Bearer tok")


if __name__ == "__main__":
    unittest.main()

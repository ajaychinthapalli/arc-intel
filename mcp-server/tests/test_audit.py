import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arc_intel.audit import CloudWatchAudit  # noqa: E402
from arc_intel.engine import Engine  # noqa: E402
from arc_intel.policy import Policy  # noqa: E402
from tests.test_engine import POLICY, _Cluster  # noqa: E402


def _row(ts, user, arn, session, verb="patch", agent="kubectl/v1.34.1"):
    return [
        {"field": "@timestamp", "value": ts}, {"field": "user.username", "value": user},
        {"field": "user.extra.arn.0", "value": arn}, {"field": "user.extra.sessionName.0", "value": session},
        {"field": "verb", "value": verb}, {"field": "userAgent", "value": agent},
        {"field": "sourceIPs.0", "value": "10.0.12.7"},
    ]


class StubLogs:
    """Mimics boto3 CloudWatch Logs: first poll Running, then Complete."""

    def __init__(self, rows, final="Complete"):
        self.rows, self.final, self.polls, self.queries = rows, final, 0, []

    def start_query(self, **kw):
        self.queries.append(kw)
        return {"queryId": "q-1"}

    def get_query_results(self, queryId):
        self.polls += 1
        if self.polls == 1:
            return {"status": "Running", "results": []}
        return {"status": self.final, "results": self.rows}

    def stop_query(self, queryId):
        pass


ROWS = [
    _row("2026-10-17 03:12:55.481", "arn:aws:sts::111122223333:assumed-role/PlatformAdmin/jdoe",
         "arn:aws:iam::111122223333:role/PlatformAdmin", "jdoe"),
    _row("2026-10-16 22:41:06.902", "arn:aws:sts::111122223333:assumed-role/BuildOnCall/asmith",
         "arn:aws:iam::111122223333:role/BuildOnCall", "asmith", verb="update"),
    _row("2026-10-01 09:00:00.100", "system:serviceaccount:argocd:argocd-application-controller", None, None,
         verb="patch", agent="helm/v3"),
]


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubLogs(ROWS)
        audit = CloudWatchAudit("/aws/eks/arc-prod/cluster", client=self.stub, poll_seconds=0)
        self.e = Engine(_Cluster(), Policy.load(POLICY), audit=audit)

    def test_drift_resolves_iam_identity(self):
        d = self.e.drift(self.e.cluster.get_ars("arc-runners", "linux-large"), with_audit=True)
        self.assertEqual(d["identity_source"], "eks_audit_log")
        by_path = {x["path"]: x for x in d["entries"]}
        image = by_path["spec.template.spec.containers[name=runner].image"]["changed_by"][0]["actor"]
        self.assertEqual(image["session"], "jdoe")
        self.assertEqual(image["iam_arn"], "arn:aws:iam::111122223333:role/PlatformAdmin")
        mx = by_path["spec.maxRunners"]["changed_by"][0]["actor"]
        self.assertEqual(mx["session"], "asmith")

    def test_query_is_scoped_and_safe(self):
        self.e.audit.trail("arc-runners", "linux-large")
        q = self.stub.queries[0]["queryString"]
        self.assertIn('objectRef.name = "linux-large"', q)
        self.assertEqual(self.stub.queries[0]["logGroupName"], "/aws/eks/arc-prod/cluster")
        with self.assertRaises(ValueError):
            self.e.audit.trail('arc-runners" or 1=1', "x")

    def test_failure_is_reported_not_raised(self):
        self.e.audit = CloudWatchAudit("/g", client=StubLogs([], final="Failed"), poll_seconds=0)
        d = self.e.drift(self.e.cluster.get_ars("arc-runners", "linux-large"), with_audit=True)
        self.assertTrue(d["identity_source"].startswith("error"))
        self.assertTrue(d["drifted"])

    def test_without_audit_configured(self):
        e = Engine(_Cluster(), Policy.load(POLICY))
        d = e.drift(e.cluster.get_ars("arc-runners", "linux-large"), with_audit=True)
        self.assertTrue(d["identity_source"].startswith("unavailable"))


if __name__ == "__main__":
    unittest.main()

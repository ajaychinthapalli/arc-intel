"""Who really made a change: EKS control-plane audit logs via CloudWatch Logs Insights.

managedFields tells us *which tool* last wrote a field (kubectl-edit, helm, argocd...).
EKS audit logs tell us *which identity*: the Kubernetes username plus the IAM role/user
ARN and session name behind it. This module looks those up for an AutoscalingRunnerSet
and matches them to drifted fields by timestamp.

Requires EKS control-plane `audit` logging and an IAM policy allowing
logs:StartQuery / logs:GetQueryResults / logs:StopQuery on /aws/eks/<cluster>/cluster
(granted to arc-intel through EKS Pod Identity).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import time
from typing import Any

log = logging.getLogger(__name__)

DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
WRITE_VERBS = ("create", "update", "patch", "delete")

QUERY = """fields @timestamp, user.username, user.extra.arn.0, user.extra.sessionName.0, verb, userAgent, sourceIPs.0, responseStatus.code
| filter @logStream like /^kube-apiserver-audit/
| filter objectRef.apiGroup = "actions.github.com" and objectRef.resource = "autoscalingrunnersets"
| filter objectRef.namespace = "{namespace}" and objectRef.name = "{name}"
| filter verb in ["create", "update", "patch", "delete"]
| filter responseStatus.code < 300
| sort @timestamp desc
| limit {limit}"""


def _parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    try:
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


class CloudWatchAudit:
    def __init__(self, log_group: str, client: Any = None, region: str | None = None,
                 poll_seconds: float = 1.0, timeout_seconds: float = 30.0):
        self.log_group = log_group
        if client is None:
            import boto3  # lazy: only needed on EKS

            client = boto3.client("logs", region_name=region or os.environ.get("AWS_REGION"))
        self.client = client
        self.poll = poll_seconds
        self.timeout = timeout_seconds

    def trail(self, namespace: str, name: str, hours: int = 24, limit: int = 50) -> list[dict]:
        """Successful write requests to one AutoscalingRunnerSet, newest first."""
        if not (DNS_LABEL.match(namespace) and DNS_LABEL.match(name)):
            raise ValueError("namespace and name must be valid Kubernetes names")
        end = int(time.time())
        start = end - max(1, min(hours, 24 * 30)) * 3600
        qid = self.client.start_query(
            logGroupName=self.log_group, startTime=start, endTime=end,
            queryString=QUERY.format(namespace=namespace, name=name, limit=max(1, min(limit, 500))),
        )["queryId"]
        deadline = time.monotonic() + self.timeout
        while True:
            res = self.client.get_query_results(queryId=qid)
            if res.get("status") in ("Complete", "Failed", "Cancelled", "Timeout"):
                break
            if time.monotonic() > deadline:
                try:
                    self.client.stop_query(queryId=qid)
                except Exception:  # noqa: BLE001
                    pass
                raise TimeoutError("CloudWatch Logs Insights query timed out")
            time.sleep(self.poll)
        if res.get("status") != "Complete":
            raise RuntimeError(f"Logs Insights query {res.get('status')}")
        rows = []
        for row in res.get("results", []):
            f = {c["field"]: c.get("value") for c in row}
            rows.append({
                "time": f.get("@timestamp"),
                "verb": f.get("verb"),
                "username": f.get("user.username"),
                "iam_arn": f.get("user.extra.arn.0"),
                "session": f.get("user.extra.sessionName.0"),
                "user_agent": f.get("userAgent"),
                "source_ip": f.get("sourceIPs.0"),
            })
        return rows


def correlate(entries: list, events: list[dict], window_seconds: int = 10) -> None:
    """Attach the audit identity whose write lands within `window_seconds` of the field
    manager's recorded time. managedFields keeps one time per manager, so this is exact for
    the most recent change by each manager."""
    parsed = [(e, _parse_ts(e.get("time"))) for e in events]
    for entry in entries:
        for cb in entry.changed_by or []:
            t = _parse_ts(cb.get("time"))
            if t is None:
                continue
            best = None
            for ev, et in parsed:
                if et is None:
                    continue
                delta = abs((et - t).total_seconds())
                if delta <= window_seconds and (best is None or delta < best[0]):
                    best = (delta, ev)
            if best:
                ev = best[1]
                cb["actor"] = {k: ev[k] for k in ("username", "iam_arn", "session", "user_agent", "source_ip", "time") if ev.get(k)}


def from_env() -> CloudWatchAudit | None:
    group = os.environ.get("EKS_AUDIT_LOG_GROUP")
    if not group:
        return None
    try:
        return CloudWatchAudit(group)
    except Exception:  # noqa: BLE001
        log.exception("CloudWatch audit lookup disabled")
        return None

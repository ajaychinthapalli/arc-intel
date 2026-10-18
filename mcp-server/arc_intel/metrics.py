"""Background scanner that exports fleet posture as Prometheus metrics."""

from __future__ import annotations

import logging
import threading
import time

from .engine import Engine

log = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge, start_http_server

    SCORE = Gauge("arc_intel_compliance_score", "Compliance score 0-100", ["namespace", "name"])
    VIOLATIONS = Gauge("arc_intel_violations", "Open policy violations", ["namespace", "name", "rule", "severity", "origin"])
    DRIFT = Gauge("arc_intel_drift_fields", "Fields drifted from source of truth", ["namespace", "name", "severity"])
    MANUAL = Gauge("arc_intel_manual_changes", "Drifted fields last written by an unexpected field manager", ["namespace", "name"])
    POSTURE = Gauge("arc_intel_posture", "1 for the scale set's current posture", ["namespace", "name", "posture"])
    LAST_SCAN = Gauge("arc_intel_last_scan_timestamp_seconds", "Unix time of last successful scan")
    ERRORS = Counter("arc_intel_scan_errors_total", "Failed scans")
    ENABLED = True
except ImportError:  # metrics are optional for local/offline use
    ENABLED = False


def scan_once(engine: Engine) -> None:
    results = [engine.assess(a) for a in engine.cluster.list_ars()]
    for g in (SCORE, VIOLATIONS, DRIFT, MANUAL, POSTURE):
        g.clear()
    for r in results:
        ns, name = r["namespace"], r["name"]
        SCORE.labels(ns, name).set(r["score"])
        POSTURE.labels(ns, name, r["posture"]).set(1)
        for v in r["violations"]:
            VIOLATIONS.labels(ns, name, v["rule_id"], v["severity"], v.get("origin", "unknown")).inc()
        drift = r.get("drift") or {}
        for sev, n in (drift.get("summary") or {}).items():
            DRIFT.labels(ns, name, sev).set(n)
        MANUAL.labels(ns, name).set(drift.get("manual_changes", 0))
    LAST_SCAN.set(time.time())


def start(engine: Engine, port: int, interval: int) -> None:
    if not ENABLED:
        log.warning("prometheus_client not installed; metrics disabled")
        return
    start_http_server(port)

    def loop():
        while True:
            try:
                scan_once(engine)
            except Exception:  # noqa: BLE001
                ERRORS.inc()
                log.exception("scan failed")
            time.sleep(interval)

    threading.Thread(target=loop, name="arc-intel-scanner", daemon=True).start()
    log.info("metrics on :%d, scanning every %ds", port, interval)

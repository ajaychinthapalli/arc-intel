"""Ask the ARC agent for a digest over A2A and post it to Slack (or stdout).

Stdlib only so it runs on a stock python image. Env:
  A2A_URL            http://kagent-controller.kagent:8083/api/a2a/kagent/arc-compliance-agent/
  DIGEST_PROMPT      the task to send
  SLACK_WEBHOOK_URL  optional; prints to stdout when unset
"""

import json
import os
import sys
import urllib.request
import uuid

A2A_URL = os.environ.get("A2A_URL", "http://kagent-controller.kagent:8083/api/a2a/kagent/arc-compliance-agent/")
PROMPT = os.environ.get("DIGEST_PROMPT", "Give me the daily ARC compliance digest.")


def post(url, body, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def texts(node):
    """Collect text parts from an A2A Task or Message result, preferring artifacts."""
    if isinstance(node, dict):
        if node.get("kind") == "text" and "text" in node:
            yield node["text"]
        for k in ("artifacts", "parts", "status", "message", "result"):
            if k in node:
                yield from texts(node[k])
    elif isinstance(node, list):
        for x in node:
            yield from texts(x)


def main():
    resp = post(A2A_URL, {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {"role": "user", "messageId": str(uuid.uuid4()),
                               "parts": [{"kind": "text", "text": PROMPT}]}},
    })
    if "error" in resp:
        sys.exit(f"A2A error: {resp['error']}")
    result = resp.get("result", {})
    out = list(texts(result.get("artifacts") or result))
    report = (out[-1] if out else "").strip() or "(agent returned no text)"
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if hook:
        post(hook, {"text": f"*ARC compliance digest*\n{report}"}, timeout=30)
        print("posted to Slack")
    else:
        print(report)


if __name__ == "__main__":
    main()

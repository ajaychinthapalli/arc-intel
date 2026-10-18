#!/usr/bin/env bash
# Run the MCP server against the bundled offline snapshot (no cluster needed),
# then call a few tools over streamable HTTP exactly as Kagent would.
set -euo pipefail
cd "$(dirname "$0")/../.."
PORT="${PORT:-8080}"

(cd mcp-server && ARC_INTEL_OFFLINE_DIR=../demo/offline ARC_INTEL_POLICY=../deploy/mcp-server/policy.yaml \
  PORT="$PORT" python3 -m arc_intel.server) &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT
for _ in $(seq 30); do curl -sf "localhost:$PORT/healthz" >/dev/null && break; sleep 0.5; done

call() {
  curl -s "localhost:$PORT/mcp" -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -H 'MCP-Protocol-Version: 2025-06-18' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" \
  | python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; print(json.dumps(r.get("structuredContent", r), indent=2))'
}

echo "### arc_fleet_report";       call arc_fleet_report '{}'
echo "### arc_assess linux-large"; call arc_assess '{"namespace":"arc-runners","name":"linux-large"}'
echo "### arc_propose_remediation"; call arc_propose_remediation '{"namespace":"arc-runners","name":"linux-large"}'

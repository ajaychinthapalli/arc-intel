# Adoption notes for ac-ws-dev-use2

- ARC chart version: 0.14.2 (controller release `arc` in `arc-systems`)
- Scale sets adopted: arc-runner-set (namespace `arc-runners`)
- GitHub config Secret: `arc-github-app` (kept; External Secrets merges Secrets Manager arc/ac-ws-dev-use2/github-app into it)
- Agent: ModelConfig `bedrock-native`, ServiceAccount `arc-readiness-agent`

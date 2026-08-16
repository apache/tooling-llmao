#!/bin/bash
set -eo pipefail
cd /workspace

# fetch launcher if needed
curl -fsSL -o /workspace/launcher.py \
  https://raw.githubusercontent.com/apache/tooling-llmao/refs/heads/main/hosting/launcher.py
chmod +x /workspace/launcher.py

# fetch servers.yaml from asfquart
curl -fsS -H "Authorization: Bearer $FLEET_KEY" \
  "$ASFQUART_URL/vllm/config/$VLLM_SET" \
  -o /workspace/servers.yaml

python /workspace/launcher.py
# or: nohup python ... &   if on-start must return

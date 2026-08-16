#!/bin/bash
set -eo pipefail
cd /workspace

# fetch launcher if needed
# curl -fsSL -o vllm_launcher.py "$LAUNCHER_URL"
# chmod +x vllm_launcher.py

# fetch servers.yaml from asfquart
curl -fsS -H "Authorization: Bearer $FLEET_KEY" \
  "$ASFQUART_URL/vllm/config/$VLLM_SET" \
  -o /workspace/servers.yaml

python /workspace/vllm_launcher.py
# or: nohup python ... &   if on-start must return

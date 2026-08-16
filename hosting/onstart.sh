#!/bin/sh
# Vast / RunPod on-start: fetch servers.yaml then exec the launcher.
# Required env: FLEET_KEY, VLLM_SET, ASFQUART_URL
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export SERVERS_YAML="${SERVERS_YAML:-/workspace/servers.yaml}"

python3 "$HERE/fetch_config.py"
exec python3 "$HERE/launcher.py"

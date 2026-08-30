#!/bin/bash
set -eo pipefail

### for now. echo what we're trying to do:
set -x

: "${DATA_DIRECTORY:?DATA_DIRECTORY must be set by the Vast template}"
mkdir -p "$DATA_DIRECTORY"
cd "$DATA_DIRECTORY"

curl -fsSL -o "$DATA_DIRECTORY/install_set.py" \
  https://raw.githubusercontent.com/apache/tooling-llmao/refs/heads/main/hosting/vast/install_set.py

# SSL_VERIFY=0: stopgap while llm.apache.org is :8443 (self-signed).
# Remove when that host is on :443 with a public CA.
export SSL_VERIFY="${SSL_VERIFY:-0}"

# Fetch set JSON and write one Supervisor program per vLLM. No long-lived
# Python parent — supervisord runs vllm serve directly.
python3 "$DATA_DIRECTORY/install_set.py"

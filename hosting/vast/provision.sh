#!/bin/bash
set -eo pipefail

### for now. echo what we're trying to do:
set -x

: "${DATA_DIRECTORY:?DATA_DIRECTORY must be set by the Vast template}"
: "${PUBLIC_IPADDR:?PUBLIC_IPADDR is set by Vast; without it the box cannot say which host it is}"
mkdir -p "$DATA_DIRECTORY"
cd "$DATA_DIRECTORY"

# The installer is shared between providers and knows none of their names.
export LLMAO_HOST="$PUBLIC_IPADDR"

curl -fsSL -o "$DATA_DIRECTORY/install_set.py" \
  https://raw.githubusercontent.com/apache/tooling-llmao/refs/heads/main/hosting/install_set.py

# Fetch set JSON and write one Supervisor program per vLLM. No long-lived
# Python parent — supervisord runs vllm serve directly. Vast's base image
# already runs supervisord, so the installer reloads it.
python3 "$DATA_DIRECTORY/install_set.py"

#!/bin/bash
set -eo pipefail

### for now. echo what we're trying to do:
set -x

mkdir -p /workspace  # doesn't exist initially; don't fail if it does
cd /workspace

curl -fsSL -o /workspace/launcher.py \
  https://raw.githubusercontent.com/apache/tooling-llmao/refs/heads/main/hosting/launcher.py

# Do not fetch model config here. vLLM is long-lived; launcher.py GETs JSON
# from asfquart when supervisor starts it.

# SSL_VERIFY=0: stopgap while llm.apache.org is :8443 (self-signed).
# Remove this environment= line when that host is on :443 with a public CA.
cat > /etc/supervisor/conf.d/vllm-launcher.conf <<EOF
[program:vllm-launcher]
command=/bin/bash -c "source /venv/main/bin/activate && python /workspace/launcher.py"
directory=/workspace
autostart=true
autorestart=true
startretries=3
environment=FLEET_KEY="$FLEET_KEY",VLLM_SET="$VLLM_SET",ASFQUART_URL="$ASFQUART_URL",SSL_VERIFY="0"
stdout_logfile=/var/log/vllm-launcher.log
stderr_logfile=/var/log/vllm-launcher.log
EOF

supervisorctl reread
supervisorctl update

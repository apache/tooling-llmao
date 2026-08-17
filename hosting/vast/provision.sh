#!/bin/bash
set -eo pipefail

### for now. echo what we're trying to do:
set -x

mkdir -p /workspace  # doesn't exist initially; don't fail if it does
cd /workspace

curl -fsSL -o /workspace/launcher.py \
  https://raw.githubusercontent.com/apache/tooling-llmao/refs/heads/main/hosting/launcher.py

curl -fsSk -H "Authorization: Bearer $FLEET_KEY" \
  "$ASFQUART_URL/vllm/config/$VLLM_SET" \
  -o /workspace/servers.yaml
  # -k for the self-signed cert — fine as a stopgap, but worth trusting the CA properly later

cat > /etc/supervisor/conf.d/vllm-launcher.conf <<'EOF'
[program:vllm-launcher]
command=/bin/bash -c "source /venv/main/bin/activate && python /workspace/launcher.py"
directory=/workspace
autostart=true
autorestart=true
startretries=3
stdout_logfile=/var/log/vllm-launcher.log
stderr_logfile=/var/log/vllm-launcher.log
EOF

supervisorctl reread
supervisorctl update

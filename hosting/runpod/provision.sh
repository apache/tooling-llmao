#!/bin/bash
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# RunPod container entrypoint: provision once, then run supervisord as PID 1.
#
# A RunPod restart keeps only the volume ($DATA_DIRECTORY, /workspace by
# default); everything else in the container is reset. So the provisioned
# state lives on the volume:
#
#   $DATA_DIRECTORY/.llmao/install_set.py    the installer that ran
#   $DATA_DIRECTORY/.llmao/conf.d/*.conf     one Supervisor unit per server
#   $DATA_DIRECTORY/.llmao/provisioned       written last, once the units are in
#
# Until `provisioned` exists, every boot fetches the installer and runs it,
# retrying until it succeeds. The usual first failure is not GitHub: it is a
# pod created before its fleet.hosts row, which llmao refuses until the row
# is added. Once provisioned, a boot fetches nothing -- not the installer,
# not /vllm/config. To change the set, delete `provisioned` and restart.
#
# supervisord's own config, socket and pidfile are NOT on the volume. They
# are rewritten on every boot, so a stale socket left by an unclean stop
# cannot trip the next start.

set -euo pipefail

log() { echo "llmao-provision: $*" >&2; }

if [ "$#" -gt 0 ]; then
  log "ignoring start-command arguments ($*); leave the template's start command empty"
fi

: "${FLEET_KEY:?FLEET_KEY must be set by the RunPod template}"
: "${ASFQUART_URL:?ASFQUART_URL must be set by the RunPod template}"
: "${RUNPOD_POD_ID:?RUNPOD_POD_ID is unset: RunPod sets it in every pod, so this is not running on one}"

export DATA_DIRECTORY="${DATA_DIRECTORY:-/workspace}"
# The installer is shared between providers and knows none of their names.
# A RunPod box is named by its pod id, not an IP: a pod whose ports are HTTP
# only has no public IP at all, and pods can share one. fleet.hosts keys a
# RunPod row by the same id.
export LLMAO_HOST="$RUNPOD_POD_ID"

INSTALLER_URL="${LLMAO_INSTALLER_URL:-https://raw.githubusercontent.com/apache/tooling-llmao/refs/heads/main/hosting/install_set.py}"
STATE="$DATA_DIRECTORY/.llmao"
CONF_D="$STATE/conf.d"
SUPERVISORD_CONF="${LLMAO_SUPERVISORD_CONF:-/etc/supervisor/supervisord.conf}"
RUN_DIR="${LLMAO_RUN_DIR:-/run/llmao}"
RETRY_FIRST_S="${LLMAO_RETRY_FIRST_S:-15}"
RETRY_MAX_S="${LLMAO_RETRY_MAX_S:-300}"

provision_once() {
  # A half-finished earlier attempt leaves units that may not match the set;
  # start from nothing so a server dropped from the set does not linger.
  rm -rf "$CONF_D"
  mkdir -p "$CONF_D"
  curl -fsSL --max-time 60 -o "$STATE/install_set.py.new" "$INSTALLER_URL" || return 1
  mv "$STATE/install_set.py.new" "$STATE/install_set.py"
  SUPERVISOR_CONF_DIR="$CONF_D" VLLM_BIN="$(command -v vllm || echo vllm)" \
    python3 "$STATE/install_set.py" --no-reload || return 1
}

mkdir -p "$STATE"
if [ -f "$STATE/provisioned" ]; then
  log "already provisioned; fetching nothing"
else
  delay="$RETRY_FIRST_S"
  until provision_once; do
    log "provisioning failed; retrying in ${delay}s"
    sleep "$delay"
    delay=$(( delay * 2 ))
    if [ "$delay" -gt "$RETRY_MAX_S" ]; then
      delay="$RETRY_MAX_S"
    fi
  done
  date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE/provisioned"
  log "provisioned"
fi

mkdir -p "$RUN_DIR" "$(dirname "$SUPERVISORD_CONF")"
cat > "$SUPERVISORD_CONF" <<EOF
; Written by llmao provision.sh on every boot; edits do not survive a restart.
[unix_http_server]
file=$RUN_DIR/supervisor.sock
chmod=0700

[supervisord]
nodaemon=true
user=root
pidfile=$RUN_DIR/supervisord.pid
logfile=$DATA_DIRECTORY/logs/supervisord.log

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix://$RUN_DIR/supervisor.sock

[include]
files = $CONF_D/*.conf
EOF
mkdir -p "$DATA_DIRECTORY/logs"

exec supervisord -n -c "$SUPERVISORD_CONF"

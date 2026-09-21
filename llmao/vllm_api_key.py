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

"""Derive a per-vLLM api_key from the fleet key (HMAC-SHA256).

Not wired into GET /vllm/config yet. Operators of hand-launched boxes run
bin/llmao-vllm-api-key. Auto-provisioned boxes will get the same string in
the config JSON later.

HMAC, not HKDF (one output). Not SHA256(secret||msg). Not a Postgres table.
fleet.key is not rotated in place; a change means redeploy every box.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from llmao.fleet import normalize_peer_ip

# Domain label: other secrets from fleet.key must use a different string.
_LABEL = "llmao.vllm.api_key"


def derive_vllm_api_key(fleet_key: str, host: str, listen_port: int) -> str:
    """sk- + urlsafe-b64(HMAC-SHA256(fleet_key, label\\nhost\\nport)), no pad."""
    key = (fleet_key or "").strip()
    if not key or key.startswith("CHANGE_ME"):
        raise ValueError("fleet.key is missing or CHANGE_ME")
    ip = normalize_peer_ip(host)
    if not ip:
        raise ValueError("host is empty")
    if not isinstance(listen_port, int) or listen_port <= 0:
        raise ValueError(f"listen_port must be a positive int, got {listen_port!r}")
    msg = f"{_LABEL}\n{ip}\n{listen_port}".encode()
    digest = hmac.new(key.encode(), msg, hashlib.sha256).digest()
    return "sk-" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

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

"""Derive a per-vLLM api_key from fleet.vllm_api_salt (HMAC-SHA256).

The salt stays in llmao config. fleet.key is only the bearer for
GET /vllm/config. A box that copied FLEET_KEY cannot compute another box's
key. GET /vllm/config returns the derived bearer; that is how Vast boxes
receive it (instance env injection does not work).

Prefix sk-vllm- marks the key as a vLLM server bearer.

HMAC, not HKDF (one output). Not SHA256(secret||msg). Not a Postgres table.
Changing the salt invalidates every derived bearer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from llmao.fleet import normalize_peer_ip

# Domain label: other secrets from fleet.vllm_api_salt must use a different string.
_LABEL = "llmao.vllm.api_key"


def derive_vllm_api_key(salt: str, host: str, listen_port: int) -> str:
    """sk-vllm- + urlsafe-b64(HMAC-SHA256(salt, label\\nhost\\nport)), no pad."""
    if not isinstance(salt, str):
        raise ValueError("fleet.vllm_api_salt is not defined")
    key = salt.strip()
    if not key or key.startswith("CHANGE_ME"):
        raise ValueError("fleet.vllm_api_salt is missing or CHANGE_ME")
    ip = normalize_peer_ip(host)
    if not ip:
        raise ValueError("host is empty")
    if not isinstance(listen_port, int) or listen_port <= 0:
        raise ValueError(f"listen_port must be a positive int, got {listen_port!r}")
    msg = f"{_LABEL}\n{ip}\n{listen_port}".encode()
    digest = hmac.new(key.encode(), msg, hashlib.sha256).digest()
    return "sk-vllm-" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

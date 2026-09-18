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

"""Find each model's direct endpoint without hardcoding it.

Shared by llmao-smoke and llmao-saturate.

Endpoints are not committed anywhere in this repo. RunPod reassigns the port
on every pod recreate -- even when the pod lands on the same machine, we saw
one host go 17140 -> 14296 -> 10755 -> 15602 across four rebuilds -- so any
committed value is stale within days, and the addresses are not something to
publish regardless.

Three sources, in order:

  1. LiteLLM's routing table, via model_info. Always current, since it is
     what the proxy actually routes to. Loopback-only, so this works on the
     gateway host and nowhere else.

  2. LLMAO_DIRECT_<MODEL> environment variables, for running from a
     workstation. Model name upper-cased with . and - as underscores:
         export LLMAO_DIRECT_QWEN3_8B=1.2.3.4:15601

  3. Nothing, in which case --direct is unavailable and the gateway checks
     still run. That is most of the value anyway.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

LITELLM = "http://127.0.0.1:4000/model/info"
ENV_FILE = "/opt/llmao/litellm.env"


def _master_key() -> str | None:
    if key := os.environ.get("LITELLM_MASTER_KEY") or os.environ.get("MK"):
        return key
    # 0440 root:root, so this only succeeds when run as root on the gateway.
    try:
        with open(ENV_FILE) as fh:
            for line in fh:
                if line.startswith("LITELLM_MASTER_KEY="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def _from_litellm() -> dict[str, str]:
    key = _master_key()
    if not key:
        return {}
    req = urllib.request.Request(LITELLM, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)["data"]
    except Exception:
        return {}

    out: dict[str, str] = {}
    for m in data:
        base = (m.get("model_info") or {}).get("asf_api_base")
        if base:
            # Last wins: if duplicate routes exist for one model_name the
            # newest is the one to use. They should not, but they have --
            # twelve for one host at one point, invisible in both UIs.
            out[m["model_name"]] = base.replace("http://", "").rstrip("/")
    return out


def _from_env() -> dict[str, str]:
    out = {}
    for k, v in os.environ.items():
        if k.startswith("LLMAO_DIRECT_") and v:
            out[k[len("LLMAO_DIRECT_") :]] = v.replace("http://", "").rstrip("/")
    return out


def _envkey(model: str) -> str:
    return re.sub(r"[.-]", "_", model).upper()


def direct_endpoints(models: list[str] | None = None) -> dict[str, str]:
    """{model_name: 'host:port'} from whichever source is available."""
    found = _from_litellm()
    env = _from_env()
    for model in models or list(found):
        if v := env.get(_envkey(model)):
            found[model] = v  # an explicit override wins
    return found


def endpoint_for(model: str) -> str | None:
    return direct_endpoints([model]).get(model)


if __name__ == "__main__":
    eps = direct_endpoints()
    if not eps:
        print(
            "no endpoints discovered\n"
            "  on the gateway host: run as root, or export MK=<master key>\n"
            "  elsewhere: export LLMAO_DIRECT_<MODEL>=host:port"
        )
    for m, hp in sorted(eps.items()):
        print(f"{m:16} {hp}")

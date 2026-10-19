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

"""RunPod's pods: what each running box costs.

httpx only. One GET of the REST API's pod list, with providers.runpod.api_key.
Each pod reports its public IP and costPerHr; a pod that is not running, or
has no public IP, is not one of our serving boxes and is left out (a stopped
pod still reports its price).

The same list carries each pod's portMappings, which a later change can use
to find RunPod public ports the way Vast's are found.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from easydict import EasyDict as edict  # noqa: N813

if TYPE_CHECKING:
    import httpx

RUNPOD_PODS_URL = "https://rest.runpod.io/v1/pods"


def price_map_from_pods(pods: Any) -> edict:
    """publicIp → the pod's rental price, dollars per hour, for running pods only."""
    prices = edict()
    for pod in pods if isinstance(pods, list) else []:
        if not isinstance(pod, dict) or pod.get("desiredStatus") != "RUNNING":
            continue
        ip = str(pod.get("publicIp") or "").strip()
        try:
            price = float(pod.get("costPerHr"))
        except (TypeError, ValueError):
            continue
        if ip:
            prices[ip] = price
    return prices


async def fetch_pods(api_key: str, *, client: httpx.AsyncClient) -> Any:
    resp = await client.get(RUNPOD_PODS_URL, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
    resp.raise_for_status()
    return resp.json()

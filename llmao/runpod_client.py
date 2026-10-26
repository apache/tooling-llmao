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

"""RunPod's pods: where each running box is, and what it costs.

httpx only. One GET of the REST API's pod list, with providers.runpod.api_key.
Every pod is kept, with its status and its public IP, which is empty while
the pod initializes and once it has stopped: a pod the list still names is
listed whatever its status, and only absence from the list is a miss for
the fleet's unlisted count. `desiredStatus` is the pod's requested state
(RUNNING, EXITED, TERMINATED), the only status the API reports.

The same list carries each pod's portMappings, container port to public
port, which is where a RunPod box's public port comes from, as Vast's comes
from its instance listing. RunPod reassigns the port on every pod recreate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from easydict import EasyDict as edict  # noqa: N813

if TYPE_CHECKING:
    import httpx

RUNPOD_PODS_URL = "https://rest.runpod.io/v1/pods"


def boxes_from_pods(pods: Any) -> edict:
    """pod id → the box: where it is (provider, pod name and id), its status, its address and its price.

    Every pod, in any status. ``host`` is the public IP, "" while the pod
    initializes or once it has stopped; ``status`` is ``desiredStatus`` in
    lower case; ``ports`` is portMappings as container port → public port
    (``{"8002": 16643}``), empty when the pod has none. A stopped pod still
    reports a price.

    No console link yet: RunPod's console URL for one pod has not been
    confirmed, so the row names the pod and an admin finds it by name.
    """
    boxes = edict()
    for pod in pods if isinstance(pods, list) else []:
        if not isinstance(pod, dict):
            continue
        box_id = str(pod.get("id") or "")
        if not box_id:
            continue
        try:
            price = float(pod.get("costPerHr"))
        except (TypeError, ValueError):
            price = None
        boxes[box_id] = edict(
            provider="RunPod",
            box_id=box_id,
            label=str(pod.get("name") or ""),
            url="",
            usd_per_hour=price,
            host=str(pod.get("publicIp") or "").strip(),
            status=str(pod.get("desiredStatus") or "").lower(),
            ports=ports_from_pod(pod),
        )
    return boxes


def ports_from_pod(pod: dict[str, Any]) -> edict:
    """Container port → public port from one pod's portMappings; a value that is not a port is dropped."""
    raw = pod.get("portMappings")
    out = edict()
    if not isinstance(raw, dict):
        return out
    for key, public in raw.items():
        # A key is the container port as a string, "8002". The pod's `ports`
        # field spells the same ports with a protocol, "8002/tcp", so a
        # protocol suffix is dropped in case the map ever carries one. The
        # key is then normalized through int so "08002" and "8002" are one
        # port, and kept as the string the server's listen port is looked
        # up by (Vast's map is keyed the same way).
        container = str(key).split("/", 1)[0]
        try:
            out[str(int(container))] = int(public)
        except (TypeError, ValueError):
            continue
    return out


async def fetch_pods(api_key: str, *, client: httpx.AsyncClient) -> Any:
    resp = await client.get(RUNPOD_PODS_URL, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
    resp.raise_for_status()
    return resp.json()

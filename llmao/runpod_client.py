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

"""RunPod's pods: where each running rental is, and what it costs.

httpx only. REST API v2 (v1 was retired on 2026-11-15), with
providers.runpod.api_key: one GET of the pod list, then one GET per running
pod, because the list leaves out `runtime`, the live port map that is the
only place a pod's public IP and public ports are reported. Every
pod is kept, with its `status` (PROVISIONING, STARTING, RUNNING, EXITED,
ERROR, TERMINATED; the actual state, where v1's desiredStatus was the
requested one) and its public IP, which is empty until a running pod's
port map appears and once it has stopped: a pod the list still names is
listed whatever its status, and only absence from the list is a miss for
the fleet's unlisted count.

The port map gives container port to public port, which is where a RunPod
rental's public port comes from, as Vast's comes from its instance
listing. RunPod reassigns the port on every pod recreate. Only the TCP
entries count: a rental's listen ports are published as `N/tcp` on
purpose, because RunPod's HTTPS proxy (`<podId>-<port>.proxy.runpod.net`)
sits behind Cloudflare, whose request timeout is shorter than a long
generation, so completions would fail at the proxy before vLLM answered.
The map's one `http` entry is RunPod's own port, not ours.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from easydict import EasyDict as edict  # noqa: N813

from llmao.rental import Rental, price

if TYPE_CHECKING:
    import httpx

_LOGGER = logging.getLogger(__name__)

RUNPOD_PODS_URL = "https://api.runpod.io/v2/pods"


def rentals_from_pods(pods: Any) -> edict:
    """pod id → Rental: where it is (pod name and id), its status, its address, its ports and its price.

    Every pod, in any status, as fetch_pods returns them: the list's pods,
    each running one with its ``runtime``. ``host`` is the public IP from
    the port map, "" until a running pod has one (a pod is RUNNING for a
    while before its map appears) and once it has stopped; ``name`` is the
    pod id, which hosting/runpod/provision.sh sends as the rental's host
    (RUNPOD_POD_ID), since a pod's address is not known until it runs;
    ``status`` is ``status`` in lower case; ``ports`` is the map's TCP
    entries as container port → public port (``{"8002": 16643}``), empty
    when the pod has none. A stopped pod still reports a price.

    No console link yet: RunPod's console URL for one pod has not been
    confirmed, so the row names the pod and an admin finds it by name.
    """
    rentals = edict()
    for pod in pods if isinstance(pods, list) else []:
        if not isinstance(pod, dict):
            continue
        pod_id = str(pod.get("id") or "")
        if not pod_id:
            continue
        ports, host = ports_from_pod(pod)
        rentals[pod_id] = Rental(
            provider="RunPod",
            id=pod_id,
            label=str(pod.get("name") or ""),
            url="",
            usd_per_hour=price(pod.get("cost")),
            host=host,
            name=pod_id,
            status=str(pod.get("status") or "").lower(),
            ports=ports,
        )
    return rentals


def ports_from_pod(pod: dict[str, Any]) -> tuple[edict, str]:
    """(container port → public port, public IP) from one pod's live port map, ``runtime.ports``.

    Only TCP entries map (see the module docstring); an entry that is not
    a port pair is dropped. The IP is the first any entry names, "" when
    the map is absent: RunPod publishes every port on the one address.
    """
    runtime = pod.get("runtime")
    entries = runtime.get("ports") if isinstance(runtime, dict) else None
    out = edict()
    host = ""
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        host = host or str(entry.get("ip") or "").strip()
        if entry.get("type") != "tcp":
            continue
        # The container port is kept as the string the server's listen
        # port is looked up by (Vast's map is keyed the same way),
        # normalized through int so "08002" and "8002" are one port.
        try:
            out[str(int(entry.get("private")))] = int(entry.get("public"))
        except (TypeError, ValueError):
            continue
    return out, host


async def fetch_pods(api_key: str, *, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Every pod, each running one with its live port map.

    One page of the list: a page holds up to 1000 pods and a fleet has
    twenty at most, so the cursor is not followed, and a second page would
    mean something is very wrong, so it is logged. The list carries no
    ``runtime``: a running pod's address and public ports come from its
    own GET, so a pass costs one request plus one per running pod, against
    RunPod's allowance of 180 a minute. The bodies are RunPod's, so plain
    dicts.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = await client.get(RUNPOD_PODS_URL, headers=headers, timeout=30.0)
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict):
        return []
    pods = [pod for pod in body.get("pods") or [] if isinstance(pod, dict)]
    if (body.get("pagination") or {}).get("hasNextPage"):
        _LOGGER.warning("runpod: more pods than one page of the list; only the first %d are read", len(pods))

    async def with_runtime(pod: dict[str, Any]) -> None:
        resp = await client.get(f"{RUNPOD_PODS_URL}/{pod['id']}", headers=headers, timeout=30.0)
        resp.raise_for_status()
        detail = resp.json()
        pod["runtime"] = (isinstance(detail, dict) and detail.get("runtime")) or None

    _ = await asyncio.gather(*(with_runtime(pod) for pod in pods if pod.get("status") == "RUNNING" and pod.get("id")))
    return pods

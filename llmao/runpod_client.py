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
only place a pod's public IP and public ports are reported; and, the first
time a card type or a network volume is seen, one GET of the catalog or
the volume for its size (fetch_hardware), since the pod names them by id
and does not carry them. Every
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
The map's one `http` entry is RunPod's own port, not ours, and its `ip` is
the proxy's address, not the machine's.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from easydict import EasyDict as edict  # noqa: N813

from llmao.rental import Rental, price

if TYPE_CHECKING:
    import httpx

_LOGGER = logging.getLogger(__name__)

RUNPOD_PODS_URL = "https://api.runpod.io/v2/pods"
RUNPOD_GPU_URL = "https://api.runpod.io/v2/catalog/gpus"
RUNPOD_VOLUME_URL = "https://api.runpod.io/v2/network-volumes"

# Two facts a pod names by id and never changes: its card type, as the
# catalog has it (`name`, the short "RTX PRO 6000" for the pod's long type
# id, and `memory`, the VRAM in GB; the pod's own `gpu.memory` is system
# RAM) and its network volume's size in GB. Each id is asked once per
# process (fetch_hardware) and kept here; a fleet has a handful of card
# types and volumes, so the catalog is never read again for one it has seen.
_CARD_BY_ID: dict[str, edict] = {}
_VOLUME_GB_BY_ID: dict[str, float] = {}


def rentals_from_pods(pods: Any, *, hardware: edict | None = None) -> edict:
    """pod id → Rental: where it is (pod name and id), its status, its address, its ports, its price, its hardware.

    Every pod, in any status, as fetch_pods returns them: the list's pods,
    each running one with its ``runtime``. ``host`` is the public IP from
    the port map, "" until a running pod has one (a pod is RUNNING for a
    while before its map appears) and once it has stopped; ``name`` is the
    pod id, which hosting/runpod/provision.sh sends as the rental's host
    (RUNPOD_POD_ID), since a pod's address is not known until it runs;
    ``status`` is ``status`` in lower case; ``ports`` is the map's TCP
    entries as container port → public port (``{"8002": 16643}``), empty
    when the pod has none. A stopped pod still reports a price.

    The hardware is the pod's ``gpu`` (card type id and count) and
    ``disk`` (the container disk, GB) plus its volume:
    ``mounts.persistent.size``, or a network volume's size, and the card's
    short name and VRAM, from ``hardware``, what fetch_hardware looked up
    (``cards_by_id``, ``volume_gb_by_id``). The card is its short name when
    the catalog gave one, else the pod's type id; the sizes are None where
    not given.

    No console link yet: RunPod's console URL for one pod has not been
    confirmed, so the row names the pod and an admin finds it by name.
    """
    hardware = hardware or edict(cards_by_id={}, volume_gb_by_id={})
    rentals = edict()
    for pod in pods if isinstance(pods, list) else []:
        if not isinstance(pod, dict):
            continue
        pod_id = str(pod.get("id") or "")
        if not pod_id:
            continue
        ports, host = ports_from_pod(pod)
        gpu = pod.get("gpu") if isinstance(pod.get("gpu"), dict) else {}
        card_id = str(gpu.get("id") or "")
        card = hardware.cards_by_id.get(card_id) or edict(name="", vram_gb=None)
        mounts = pod.get("mounts") if isinstance(pod.get("mounts"), dict) else {}
        persistent = mounts.get("persistent") if isinstance(mounts.get("persistent"), dict) else {}
        volume_gb = price(persistent.get("size"))
        if volume_gb is None:
            volume_gb = hardware.volume_gb_by_id.get(network_volume_id(pod))
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
            gpu=card.name or card_id,
            gpus=int(gpu.get("count") or 0),
            vram_gb=card.vram_gb,
            disk_gb=price(pod.get("disk")),
            volume_gb=volume_gb,
        )
    return rentals


def network_volume_id(pod: dict[str, Any]) -> str:
    """The id of the network volume a pod mounts (``mounts.network[0].volumeId``), "" when it has none."""
    mounts = pod.get("mounts") if isinstance(pod.get("mounts"), dict) else {}
    network = mounts.get("network")
    first = network[0] if isinstance(network, list) and network and isinstance(network[0], dict) else {}
    return str(first.get("volumeId") or "")


async def fetch_hardware(api_key: str, pods: list[dict[str, Any]], *, client: httpx.AsyncClient) -> edict:
    """Each card type (short name, VRAM) and each network volume's size the pods name: looked up once, then kept.

    One GET per card type or volume id not seen before in this process
    (the module's two caches), none at all on a read that names only
    known ones. Returns ``edict(cards_by_id, volume_gb_by_id)`` for
    rentals_from_pods, ``cards_by_id`` mapping a type id to
    ``edict(name, vram_gb)``. Raises on a lookup that fails, as fetch_pods
    does, so the read is retried whole next interval.
    """
    headers = {"Authorization": f"Bearer {api_key}"}

    async def get(url: str, key: str) -> dict[str, Any]:
        # A card type's id has spaces ("NVIDIA GeForce RTX 4090"), so it is quoted.
        resp = await client.get(f"{url}/{quote(key, safe='')}", headers=headers, timeout=30.0)
        resp.raise_for_status()
        body = resp.json()
        return body if isinstance(body, dict) else {}

    async def look_up_card(card_id: str) -> None:
        body = await get(RUNPOD_GPU_URL, card_id)
        _CARD_BY_ID[card_id] = edict(name=str(body.get("name") or ""), vram_gb=price(body.get("memory")))

    async def look_up_volume(volume_id: str) -> None:
        size = price((await get(RUNPOD_VOLUME_URL, volume_id)).get("size"))
        if size is not None:
            _VOLUME_GB_BY_ID[volume_id] = size

    cards = {str(pod["gpu"].get("id") or "") for pod in pods if isinstance(pod.get("gpu"), dict)}
    volumes = {network_volume_id(pod) for pod in pods}
    lookups = [look_up_card(c) for c in cards if c and c not in _CARD_BY_ID]
    lookups += [look_up_volume(v) for v in volumes if v and v not in _VOLUME_GB_BY_ID]
    _ = await asyncio.gather(*lookups)
    return edict(cards_by_id=dict(_CARD_BY_ID), volume_gb_by_id=dict(_VOLUME_GB_BY_ID))


def ports_from_pod(pod: dict[str, Any]) -> tuple[edict, str]:
    """(container port → public port, public IP) from one pod's live port map, ``runtime.ports``.

    Only TCP entries count (see the module docstring), for the address
    too: the ``http`` entry's ``ip`` is the proxy's address, not the
    machine's, and it is often the first entry. An entry that is not a
    port pair is dropped. The IP is "" when the map is absent.
    """
    runtime = pod.get("runtime")
    entries = runtime.get("ports") if isinstance(runtime, dict) else None
    out = edict()
    host = ""
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("type") != "tcp":
            continue
        host = host or str(entry.get("ip") or "").strip()
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

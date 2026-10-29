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

"""Vast.ai show-instances: each box's public ports, where it is, and what it costs.

httpx only (no vastai SDK). One GET, no pagination; the fleet reads the
body twice, for ports (port_map_from_body) and rentals (rentals_from_body).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from easydict import EasyDict as edict  # noqa: N813

from llmao.rental import Rental, price

if TYPE_CHECKING:
    import httpx

_LOGGER = logging.getLogger(__name__)

VAST_INSTANCES_URL = "https://console.vast.ai/api/v1/instances/"
VAST_PAGE_LIMIT = 25


def _ip(addr: Any) -> str:
    ip = str(addr or "").strip()
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return ip


def ports_from_instance(inst: dict[str, Any]) -> edict:
    """Container listen port → Vast public HostPort from one instance row."""
    raw = inst.get("ports")
    out = edict()
    if not isinstance(raw, dict):
        return out
    for key, mappings in raw.items():
        listen_s = str(key).split("/", 1)[0]
        try:
            listen = int(listen_s)
            if not mappings or not isinstance(mappings, list):
                continue
            first = mappings[0]
            if not isinstance(first, dict) or first.get("HostPort") is None:
                continue
            out[str(listen)] = int(first["HostPort"])
        except (TypeError, ValueError):
            continue
    return out


def port_map_from_body(body: Any) -> edict:
    """public_ipaddr → {listen: HostPort}. Warn if the page is truncated."""
    if not isinstance(body, dict):
        raise ValueError("vast instances response must be an object")
    instances = body.get("instances") or []
    if not isinstance(instances, list):
        raise ValueError("vast instances must be a list")
    total = body.get("total_instances")
    if total is not None:
        try:
            ntotal = int(total)
        except (TypeError, ValueError):
            ntotal = None
        if ntotal is not None and ntotal > len(instances):
            _LOGGER.warning("vast: total_instances=%s but page has %s; not paginating", ntotal, len(instances))
    mapping = edict()
    for inst in instances:
        if not isinstance(inst, dict):
            continue
        ip = _ip(inst.get("public_ipaddr"))
        if not ip:
            continue
        ports = ports_from_instance(inst)
        if not ports:
            continue
        # One IP can show up twice. Keep every listen port; do not replace.
        if ip in mapping:
            mapping[ip].update(ports)
        else:
            mapping[ip] = ports
    return mapping


# Where an admin finds Vast instances. Vast has no stable page per instance;
# the instance id on the row is what to look for there.
VAST_CONSOLE_URL = "https://cloud.vast.ai/instances/"


def rentals_from_body(body: Any) -> edict:
    """instance id → Rental: where it is (instance id, GPUs), its status, its address, its ports and its price.

    Every instance, in any status: ``host`` is public_ipaddr, "" when Vast
    has not given one, and ``name`` is the same address, since
    hosting/vast/provision.sh sends PUBLIC_IPADDR as the box's host;
    ``status`` is actual_status (running, loading,
    exited, ...), the observed state; ``ports`` is container listen port →
    public HostPort, as ports_from_instance reads it.

    usd_per_hour is dph_total, which includes the instance's storage;
    bandwidth is billed per GB apart from it and is left out. None when
    Vast does not say, which is unknown, not free.
    """
    rentals = edict()
    for inst in (body.get("instances") or []) if isinstance(body, dict) else []:
        if not isinstance(inst, dict):
            continue
        instance_id = str(inst.get("id") or "")
        if not instance_id:
            continue
        gpus = inst.get("num_gpus") or 1
        gpu = str(inst.get("gpu_name") or "GPU")
        host = _ip(inst.get("public_ipaddr"))
        rentals[instance_id] = Rental(
            provider="Vast",
            id=instance_id,
            label=f"{gpu} x{gpus}",
            url=VAST_CONSOLE_URL,
            usd_per_hour=price(inst.get("dph_total")),
            host=host,
            name=host,
            status=str(inst.get("actual_status") or "").lower(),
            ports=ports_from_instance(inst),
        )
    return rentals


async def fetch_instances(api_key: str, *, client: httpx.AsyncClient) -> Any:
    """The show-instances body, for port_map_from_body and rentals_from_body."""
    resp = await client.get(
        VAST_INSTANCES_URL,
        params={"limit": VAST_PAGE_LIMIT},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()

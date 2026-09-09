"""Vast.ai show-instances: map public IP + listen port to HostPort.

httpx only (no vastai SDK). One GET, no pagination. Returns EasyDict.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from easydict import EasyDict as edict

_LOGGER = logging.getLogger(__name__)

VAST_INSTANCES_URL = "https://console.vast.ai/api/v1/instances"
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
            _LOGGER.warning(
                f"vast: total_instances={ntotal} but page has {len(instances)}; not paginating"
            )
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
        mapping[ip] = ports
    return mapping


async def fetch_port_map(api_key: str, *, client: httpx.AsyncClient) -> edict:
    resp = await client.get(
        VAST_INSTANCES_URL,
        params={"limit": VAST_PAGE_LIMIT},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return port_map_from_body(resp.json())

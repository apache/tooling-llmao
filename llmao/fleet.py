"""Build a vLLM set config JSON from config.yaml fleet.sets + model_list.

GPU boxes fetch this JSON at provision/install time (no servers.yaml).
"""
from __future__ import annotations

from typing import Any

from llmao.models import load_model_list


class UnknownSet(KeyError):
    """No fleet.sets entry for this set id."""


def _vllm_block(entry: dict[str, Any]) -> dict[str, Any]:
    info = entry.get("model_info") or {}
    block = info.get("vllm")
    if not isinstance(block, dict) or not block:
        raise ValueError(
            f"{entry.get('model_name')}: model_info.vllm is required for fleet models"
        )
    return block


def _mapping(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "items"):
        return dict(obj.items())
    return None


def _sets_from_cfg(cfg: Any) -> dict[str, Any]:
    if cfg is None:
        raise ValueError("config_for_set requires cfg (fleet.sets)")
    fleet = getattr(cfg, "fleet", None)
    if fleet is None and isinstance(cfg, dict):
        fleet = cfg.get("fleet")
    raw = None
    if fleet is not None:
        raw = fleet.get("sets") if hasattr(fleet, "get") else getattr(fleet, "sets", None)
    sets = _mapping(raw)
    if not sets:
        return {}
    return {str(k): v for k, v in sets.items()}


def _catalog_by_name(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for entry in entries:
        name = entry.get("model_name")
        if not name:
            raise ValueError("model_list entry missing model_name")
        key = str(name)
        if key in out:
            raise ValueError(f"duplicate model_name in model_list: {key}")
        out[key] = entry
    return out


def _server_from_catalog(
    entry: dict[str, Any],
    *,
    name: str,
    host: str,
    port: int,
) -> dict[str, Any]:
    catalog = entry.get("model_name")
    params = entry.get("litellm_params") or {}
    api_key = params.get("api_key")
    if not api_key:
        raise ValueError(f"{catalog}: litellm_params.api_key is required")
    vllm = _vllm_block(entry)
    model = vllm.get("model")
    if not model:
        raise ValueError(f"{catalog}: model_info.vllm needs model")
    server: dict[str, Any] = {
        "name": str(name),
        "model": str(model),
        "host": str(host),
        "port": int(port),
        "api_key": str(api_key),
    }
    if vllm.get("gpu_memory_utilization") is not None:
        server["gpu_memory_utilization"] = float(vllm["gpu_memory_utilization"])
    if vllm.get("max_model_len") is not None:
        server["max_model_len"] = int(vllm["max_model_len"])
    args = vllm.get("args") or []
    if isinstance(args, str):
        args = args.split()
    server["args"] = [str(a) for a in args]
    return server


def _set_item(item: Any, index: int) -> dict[str, Any]:
    row = _mapping(item)
    if row is None:
        raise ValueError(f"fleet.sets item {index} must be a mapping with model, host, port")
    model = str(row.get("model") or "").strip()
    host = str(row.get("host") or "").strip()
    port = row.get("port")
    if not model or not host or port is None or str(port).strip() == "":
        raise ValueError(f"fleet.sets item {index} needs model, host, and port")
    name = str(row.get("name") or model).strip()
    if not name:
        raise ValueError(f"fleet.sets item {index} has empty name")
    return {"model": model, "host": host, "port": int(port), "name": name}


def config_for_set(set_id: str, *, entries: list[dict[str, Any]] | None = None, cfg: Any = None) -> dict[str, Any]:
    """JSON object for one set. Raises UnknownSet if the id is missing."""
    if not set_id:
        raise UnknownSet(set_id)
    sets = _sets_from_cfg(cfg)
    if set_id not in sets:
        raise UnknownSet(set_id)
    items = sets[set_id]
    if items is None:
        raise UnknownSet(set_id)
    if hasattr(items, "values") and not isinstance(items, (list, tuple, str)):
        raise ValueError(f"fleet.sets.{set_id} must be a list of servers")
    rows = list(items)
    if not rows:
        raise UnknownSet(set_id)

    catalog = _catalog_by_name(entries if entries is not None else load_model_list(cfg=cfg))
    servers = []
    names: set[str] = set()
    places: set[tuple[str, int]] = set()
    for i, raw in enumerate(rows):
        spec = _set_item(raw, i)
        entry = catalog.get(spec["model"])
        if entry is None:
            raise ValueError(f"fleet.sets.{set_id}[{i}]: unknown model {spec['model']!r}")
        if spec["name"] in names:
            raise ValueError(f"fleet.sets.{set_id}: duplicate name {spec['name']!r}")
        place = (spec["host"], spec["port"])
        if place in places:
            raise ValueError(
                f"fleet.sets.{set_id}: duplicate {spec['host']}:{spec['port']}"
            )
        names.add(spec["name"])
        places.add(place)
        servers.append(
            _server_from_catalog(
                entry, name=spec["name"], host=spec["host"], port=spec["port"]
            )
        )
    return {
        "set_id": set_id,
        "hf_home": "/workspace/hf-cache",
        "log_dir": "/workspace/logs",
        "servers": servers,
    }

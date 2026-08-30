"""vLLM set JSON: validate at process start, join catalog at request time.

GPU boxes fetch GET /vllm/config/{set_id} (no servers.yaml).
"""
from __future__ import annotations

from typing import Any

from llmao.models import load_model_list


class UnknownSet(KeyError):
    """No fleet.sets entry for this set id."""


def _item_name(item: Any) -> str:
    return str(item.get("name") or item.model)


def _server_from_catalog(entry: Any, item: Any) -> dict[str, Any]:
    vllm = entry.model_info.vllm
    args = vllm.get("args") or []
    if isinstance(args, str):
        args = args.split()
    server = {
        "name": _item_name(item),
        "model": str(vllm.model),
        "host": str(item.host),
        "port": int(item.port),
        "api_key": str(entry.litellm_params.api_key),
        "args": [str(a) for a in args],
    }
    if vllm.get("gpu_memory_utilization") is not None:
        server["gpu_memory_utilization"] = float(vllm.gpu_memory_utilization)
    if vllm.get("max_model_len") is not None:
        server["max_model_len"] = int(vllm.max_model_len)
    return server


def validate_fleet(cfg: Any, entries: list | None = None) -> None:
    """Fail-fast if fleet.sets or the catalog cannot be joined. Call at startup."""
    if "fleet" not in cfg:
        raise ValueError("config.yaml: missing fleet")
    if "sets" not in cfg.fleet:
        raise ValueError("config.yaml: missing fleet.sets")
    sets = cfg.fleet.sets
    if not hasattr(sets, "items"):
        raise ValueError("config.yaml: fleet.sets must be a mapping")

    rows = entries if entries is not None else load_model_list(cfg=cfg)
    catalog_names: list[str] = []
    for entry in rows:
        if "model_name" not in entry or not entry.model_name:
            raise ValueError("model_list entry missing model_name")
        name = str(entry.model_name)
        if name in catalog_names:
            raise ValueError(f"duplicate model_name in model_list: {name}")
        catalog_names.append(name)
        if "model_info" not in entry or "vllm" not in entry.model_info:
            raise ValueError(f"{name}: model_info.vllm is required")
        vllm = entry.model_info.vllm
        if "model" not in vllm or not vllm.model:
            raise ValueError(f"{name}: model_info.vllm.model is required")
        if "litellm_params" not in entry or not entry.litellm_params.api_key:
            raise ValueError(f"{name}: litellm_params.api_key is required")

    catalog = set(catalog_names)
    for set_id, items in sets.items():
        if not isinstance(items, (list, tuple)):
            raise ValueError(f"fleet.sets.{set_id} must be a list of servers")
        seen_names: set[str] = set()
        seen_places: set[tuple[str, int]] = set()
        for i, item in enumerate(items):
            if "model" not in item or "host" not in item or "port" not in item:
                raise ValueError(
                    f"fleet.sets.{set_id}[{i}] needs model, host, and port"
                )
            model = str(item.model).strip()
            host = str(item.host).strip()
            if not model or not host or item.port is None or str(item.port).strip() == "":
                raise ValueError(
                    f"fleet.sets.{set_id}[{i}] needs model, host, and port"
                )
            if model not in catalog:
                raise ValueError(f"fleet.sets.{set_id}[{i}]: unknown model {model!r}")
            label = _item_name(item)
            if label in seen_names:
                raise ValueError(f"fleet.sets.{set_id}: duplicate name {label!r}")
            place = (host, item.port)
            if place in seen_places:
                raise ValueError(f"fleet.sets.{set_id}: duplicate {host}:{item.port}")
            seen_names.add(label)
            seen_places.add(place)


def config_for_set(
    set_id: str,
    *,
    entries: list | None = None,
    cfg: Any = None,
) -> dict[str, Any]:
    """JSON for one set. Requires validate_fleet() already ran on cfg."""
    if not set_id or set_id not in cfg.fleet.sets:
        raise UnknownSet(set_id)
    rows = entries if entries is not None else load_model_list(cfg=cfg)
    catalog = {entry.model_name: entry for entry in rows}
    servers = [
        _server_from_catalog(catalog[item.model], item) for item in cfg.fleet.sets[set_id]
    ]
    return {
        "set_id": set_id,
        "hf_home": "/workspace/hf-cache",
        "log_dir": "/workspace/logs",
        "servers": servers,
    }

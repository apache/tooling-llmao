"""Build a vLLM set config JSON from model_list.yaml.

The GPU-box launcher fetches this at process start (no servers.yaml).
"""
from __future__ import annotations

from typing import Any

from llmao.models import load_model_list


class UnknownSet(KeyError):
    """No model_list entry has this model_set."""


def _vllm_block(entry: dict[str, Any]) -> dict[str, Any]:
    info = entry.get("model_info") or {}
    block = info.get("vllm")
    if not isinstance(block, dict) or not block:
        raise ValueError(
            f"{entry.get('model_name')}: model_info.vllm is required for fleet models"
        )
    return block


def _server_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    name = entry.get("model_name")
    if not name:
        raise ValueError("model_list entry missing model_name")
    params = entry.get("litellm_params") or {}
    api_key = params.get("api_key")
    if not api_key:
        raise ValueError(f"{name}: litellm_params.api_key is required")
    vllm = _vllm_block(entry)
    model = vllm.get("model")
    port = vllm.get("port")
    if not model or port is None:
        raise ValueError(f"{name}: model_info.vllm needs model and port")
    server: dict[str, Any] = {
        "name": str(name),
        "model": str(model),
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


def config_for_set(set_id: str, *, entries: list[dict[str, Any]] | None = None, cfg: Any = None) -> dict[str, Any]:
    """JSON object for one model_set. Raises UnknownSet if empty."""
    if not set_id:
        raise UnknownSet(set_id)
    rows = entries if entries is not None else load_model_list(cfg=cfg)
    servers = []
    for entry in rows:
        info = entry.get("model_info") or {}
        if str(info.get("model_set") or "") != set_id:
            continue
        servers.append(_server_from_entry(entry))
    if not servers:
        raise UnknownSet(set_id)
    return {
        "set_id": set_id,
        "hf_home": "/workspace/hf-cache",
        "log_dir": "/workspace/logs",
        "servers": servers,
    }

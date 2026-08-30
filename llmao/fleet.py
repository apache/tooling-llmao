"""vLLM set JSON: validate at process start, join catalog at request time.

GPU boxes fetch GET /vllm/config/{set_id} (no servers.yaml).

Terminology: the **catalog** is model_list.yaml (how to serve). A **model** is
one catalog recipe (`model_name`). A **server** is one vLLM process in a set
(host + port). Box JSON `servers[].model` is still the HF weights id.
"""
from __future__ import annotations

from typing import Any

from llmao.models import load_model_list


class UnknownSet(KeyError):
    """No fleet.sets entry for this set id."""


def _spec_name(spec: Any) -> str:
    return str(spec.get("name") or spec.model)


def _server(model: Any, spec: Any) -> dict[str, Any]:
    vllm = model.model_info.vllm
    args = vllm.get("args") or []
    if isinstance(args, str):
        args = args.split()
    server = {
        "name": _spec_name(spec),
        "model": str(vllm.model),
        "host": str(spec.host),
        "port": int(spec.port),
        "api_key": str(model.litellm_params.api_key),
        "args": [str(a) for a in args],
    }
    if vllm.get("gpu_memory_utilization") is not None:
        server["gpu_memory_utilization"] = float(vllm.gpu_memory_utilization)
    if vllm.get("max_model_len") is not None:
        server["max_model_len"] = int(vllm.max_model_len)
    return server


def validate_fleet(cfg: Any, models: list | None = None) -> None:
    """Fail-fast if fleet.sets or the catalog cannot be joined. Call at startup."""
    if "fleet" not in cfg:
        raise ValueError("config.yaml: missing fleet")
    if "sets" not in cfg.fleet:
        raise ValueError("config.yaml: missing fleet.sets")
    sets = cfg.fleet.sets
    if not hasattr(sets, "items"):
        raise ValueError("config.yaml: fleet.sets must be a mapping")

    models = models if models is not None else load_model_list(cfg=cfg)
    names: list[str] = []
    for model in models:
        if "model_name" not in model or not model.model_name:
            raise ValueError("catalog model missing model_name")
        name = str(model.model_name)
        if name in names:
            raise ValueError(f"duplicate model_name in catalog: {name}")
        names.append(name)
        if "model_info" not in model or "vllm" not in model.model_info:
            raise ValueError(f"{name}: model_info.vllm is required")
        vllm = model.model_info.vllm
        if "model" not in vllm or not vllm.model:
            raise ValueError(f"{name}: model_info.vllm.model is required")
        if "litellm_params" not in model or not model.litellm_params.api_key:
            raise ValueError(f"{name}: litellm_params.api_key is required")

    catalog = set(names)
    for set_id, specs in sets.items():
        if not isinstance(specs, (list, tuple)):
            raise ValueError(f"fleet.sets.{set_id} must be a list of servers")
        seen_names: set[str] = set()
        seen_places: set[tuple[str, int]] = set()
        for i, spec in enumerate(specs):
            if "model" not in spec or "host" not in spec or "port" not in spec:
                raise ValueError(
                    f"fleet.sets.{set_id}[{i}] needs model, host, and port"
                )
            model_name = str(spec.model).strip()
            host = str(spec.host).strip()
            if not model_name or not host or spec.port is None or str(spec.port).strip() == "":
                raise ValueError(
                    f"fleet.sets.{set_id}[{i}] needs model, host, and port"
                )
            if model_name not in catalog:
                raise ValueError(
                    f"fleet.sets.{set_id}[{i}]: unknown model {model_name!r}"
                )
            label = _spec_name(spec)
            if label in seen_names:
                raise ValueError(f"fleet.sets.{set_id}: duplicate name {label!r}")
            place = (host, spec.port)
            if place in seen_places:
                raise ValueError(f"fleet.sets.{set_id}: duplicate {host}:{spec.port}")
            seen_names.add(label)
            seen_places.add(place)


def config_for_set(
    set_id: str,
    *,
    models: list | None = None,
    cfg: Any = None,
) -> dict[str, Any]:
    """JSON for one set. Requires validate_fleet() already ran on cfg."""
    if not set_id or set_id not in cfg.fleet.sets:
        raise UnknownSet(set_id)
    models = models if models is not None else load_model_list(cfg=cfg)
    by_name = {model.model_name: model for model in models}
    servers = [
        _server(by_name[spec.model], spec) for spec in cfg.fleet.sets[set_id]
    ]
    return {
        "set_id": set_id,
        "hf_home": "/workspace/hf-cache",
        "log_dir": "/workspace/logs",
        "servers": servers,
    }

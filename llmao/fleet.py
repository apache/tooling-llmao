"""vLLM set JSON: validate at process start, join catalog at request time.

GPU boxes fetch GET /vllm/config/{set_id} (no servers.yaml).

Terminology: the **catalog** is model_list.yaml (how to serve). A **model** is
one catalog recipe (`model_name`). A **server** is one vLLM process in a set
(host + port). Box JSON `servers[].model` is still the HF weights id.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from llmao.models import load_model_list

_LOGGER = logging.getLogger(__name__)

_FLEET_NUMBERS = (
    "health_interval_s",
    "health_timeout_s",
    "health_grace_s",
    "health_fail_threshold",
    "skew_interval_s",
    "litellm_health_interval_s",
)


class UnknownSet(KeyError):
    """No fleet.sets entry for this set id."""


def _spec_name(spec: Any) -> str:
    return str(spec.get("name") or spec.model)


def validate_fleet(cfg: Any, models: list | None = None) -> None:
    """Fail-fast if fleet.sets or the catalog cannot be joined. Call at startup."""
    if "fleet" not in cfg:
        raise ValueError("config.yaml: missing fleet")
    if "sets" not in cfg.fleet:
        raise ValueError("config.yaml: missing fleet.sets")
    sets = cfg.fleet.sets
    if not hasattr(sets, "items"):
        raise ValueError("config.yaml: fleet.sets must be a mapping")
    for key in _FLEET_NUMBERS:
        if key not in cfg.fleet:
            raise ValueError(f"config.yaml: missing fleet.{key}")
        raw = cfg.fleet[key]
        try:
            val = int(raw) if key == "health_fail_threshold" else float(raw)
        except (TypeError, ValueError) as e:
            raise ValueError(f"config.yaml: fleet.{key} must be a number") from e
        if val <= 0:
            raise ValueError(f"config.yaml: fleet.{key} must be > 0")

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
        Server.from_spec(str(set_id), spec, by_name[spec.model]).box_json()
        for spec in cfg.fleet.sets[set_id]
    ]
    return {
        "set_id": set_id,
        "hf_home": "/workspace/hf-cache",
        "log_dir": "/workspace/logs",
        "servers": servers,
    }


class Server:
    """One vLLM process from fleet.sets (live health + box JSON)."""

    UNKNOWN = "unknown"
    STARTING = "starting"
    HEALTHY = "healthy"
    DOWN = "down"

    def __init__(
        self,
        *,
        set_id: str,
        model_name: str,
        name: str,
        host: str,
        port: int,
        hf_model: str,
        api_key: str,
        args: list[str],
        gpu_memory_utilization: float | None = None,
        max_model_len: int | None = None,
    ):
        self.set_id = set_id
        self.model_name = model_name
        self.name = name
        self.host = host
        self.port = int(port)
        self.hf_model = hf_model
        self.api_key = api_key
        self.args = args
        self.gpu_memory_utilization = gpu_memory_utilization
        self.max_model_len = max_model_len
        self.state = self.UNKNOWN
        self.seen_at = time.time()
        self.last_ok = None
        self.last_error = None
        self.fails = 0
        self.skew: list[str] = []

    @classmethod
    def from_spec(cls, set_id: str, spec: Any, model: Any) -> Server:
        vllm = model.model_info.vllm
        args = vllm.get("args") or []
        if isinstance(args, str):
            args = args.split()
        util = vllm.get("gpu_memory_utilization")
        maxlen = vllm.get("max_model_len")
        return cls(
            set_id=set_id,
            model_name=str(spec.model).strip(),
            name=_spec_name(spec),
            host=str(spec.host).strip(),
            port=int(spec.port),
            hf_model=str(vllm.model),
            api_key=str(model.litellm_params.api_key),
            args=[str(a) for a in args],
            gpu_memory_utilization=float(util) if util is not None else None,
            max_model_len=int(maxlen) if maxlen is not None else None,
        )

    @property
    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/health"

    @property
    def api_base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def box_json(self) -> dict[str, Any]:
        """Wire payload for GET /vllm/config (servers[].model is the HF weights id)."""
        out: dict[str, Any] = {
            "name": self.name,
            "model": self.hf_model,
            "host": self.host,
            "port": self.port,
            "api_key": self.api_key,
            "args": list(self.args),
        }
        if self.gpu_memory_utilization is not None:
            out["gpu_memory_utilization"] = self.gpu_memory_utilization
        if self.max_model_len is not None:
            out["max_model_len"] = self.max_model_len
        return out

    def record_probe(
        self,
        ok: bool,
        *,
        now: float,
        grace_s: float,
        fail_threshold: int,
        err: str | None = None,
    ) -> None:
        if ok:
            if self.state != self.HEALTHY:
                _LOGGER.info(f"fleet server {self.name}@{self.api_base} healthy")
            self.state = self.HEALTHY
            self.last_ok = now
            self.last_error = None
            self.fails = 0
            return
        self.fails += 1
        self.last_error = err or "unhealthy"
        if self.state == self.HEALTHY:
            if self.fails >= fail_threshold:
                self.state = self.DOWN
                _LOGGER.warning(
                    f"fleet server {self.name}@{self.api_base} down ({self.last_error})"
                )
            return
        if (now - self.seen_at) < grace_s:
            self.state = self.STARTING
            return
        if self.state != self.DOWN:
            _LOGGER.warning(
                f"fleet server {self.name}@{self.api_base} still not healthy after grace ({self.last_error})"
            )
        self.state = self.DOWN


class Fleet:
    """Live fleet: servers from config, health from probes, config-fetch stamps."""

    BADGE_UP = "up"
    BADGE_STARTING = "starting"
    BADGE_DOWN = "down"
    BADGE_MIXED = "mixed"

    def __init__(self, cfg: Any, servers: list[Server]):
        self.cfg = cfg
        self.servers = servers
        self.config_fetch_at: dict[str, float] = {}

    @classmethod
    def from_cfg(cls, cfg: Any, models: list | None = None) -> Fleet:
        models = models if models is not None else load_model_list(cfg=cfg)
        by_name = {model.model_name: model for model in models}
        servers = []
        for set_id, specs in cfg.fleet.sets.items():
            for spec in specs:
                servers.append(Server.from_spec(str(set_id), spec, by_name[spec.model]))
        return cls(cfg, servers)

    def note_config_fetch(self, set_id: str, *, now: float | None = None) -> None:
        self.config_fetch_at[set_id] = now if now is not None else time.time()

    def model_health(self, model_name: str) -> str:
        """Aggregate: up / starting / down / mixed, or empty if no servers."""
        states = [s.state for s in self.servers if s.model_name == model_name]
        if not states:
            return ""
        uniq = set(states)
        if uniq == {Server.HEALTHY}:
            return self.BADGE_UP
        if uniq <= {Server.STARTING, Server.UNKNOWN}:
            return self.BADGE_STARTING
        if uniq == {Server.DOWN}:
            return self.BADGE_DOWN
        if Server.HEALTHY in uniq and uniq <= {Server.HEALTHY, Server.STARTING, Server.UNKNOWN}:
            return self.BADGE_UP
        return self.BADGE_MIXED

    async def probe_all(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        now: float | None = None,
    ) -> None:
        timeout = float(self.cfg.fleet.health_timeout_s)
        grace = float(self.cfg.fleet.health_grace_s)
        threshold = int(self.cfg.fleet.health_fail_threshold)
        stamp = now if now is not None else time.time()
        own = client is None
        if own:
            client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        try:
            for srv in self.servers:
                ok, err = await _get_health(client, srv.health_url)
                srv.record_probe(
                    ok, now=stamp, grace_s=grace, fail_threshold=threshold, err=err
                )
        finally:
            if own:
                await client.aclose()

    async def run_health(self) -> None:
        interval = float(self.cfg.fleet.health_interval_s)
        while True:
            try:
                await self.probe_all()
            except Exception:
                _LOGGER.exception("fleet health probe failed")
            await asyncio.sleep(interval)


async def _get_health(client: httpx.AsyncClient, url: str) -> tuple[bool, str | None]:
    try:
        resp = await client.get(url)
    except httpx.HTTPError as e:
        return False, str(e)
    if resp.status_code == 200:
        return True, None
    return False, f"HTTP {resp.status_code}"

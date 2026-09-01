"""vLLM host JSON: validate at process start, join catalog at request time.

GPU boxes fetch GET /vllm/config (no servers.yaml). Placement is fleet.hosts
keyed by client IP.

Terminology: the **catalog** is model_list.yaml (how to serve). A **model** is
one catalog recipe (`model_name`). A **server** is one vLLM process on a host
(port). Box JSON `servers[].model` is still the HF weights id.
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


class UnknownHost(KeyError):
    """No fleet.hosts entry for this client IP."""


def normalize_peer_ip(addr: str | None) -> str:
    ip = (addr or "").strip()
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return ip


def parse_host_row(raw: Any, host: str, index: int) -> tuple[str, int, str]:
    if not isinstance(raw, (list, tuple)) or len(raw) not in (2, 3):
        raise ValueError(
            f"fleet.hosts.{host}[{index}] must be [model, port] or [model, port, name]"
        )
    model_name = str(raw[0]).strip()
    try:
        port = int(raw[1])
    except (TypeError, ValueError) as e:
        raise ValueError(f"fleet.hosts.{host}[{index}] port must be an int") from e
    name = str(raw[2]).strip() if len(raw) == 3 else model_name
    if not model_name or not name:
        raise ValueError(f"fleet.hosts.{host}[{index}] needs model and name")
    return model_name, port, name


def validate_fleet(cfg: Any, models: list | None = None) -> None:
    """Fail-fast if fleet.hosts or the catalog cannot be joined. Call at startup."""
    if "fleet" not in cfg:
        raise ValueError("config.yaml: missing fleet")
    if "hosts" not in cfg.fleet:
        raise ValueError("config.yaml: missing fleet.hosts")
    hosts = cfg.fleet.hosts
    if not hasattr(hosts, "items"):
        raise ValueError("config.yaml: fleet.hosts must be a mapping")
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
    for host, rows in hosts.items():
        host = str(host).strip()
        if not host:
            raise ValueError("fleet.hosts has an empty IP key")
        if not isinstance(rows, (list, tuple)):
            raise ValueError(f"fleet.hosts.{host} must be a list of [model, port] rows")
        seen_names: set[str] = set()
        seen_ports: set[int] = set()
        for i, raw in enumerate(rows):
            model_name, port, label = parse_host_row(raw, host, i)
            if model_name not in catalog:
                raise ValueError(f"fleet.hosts.{host}[{i}]: unknown model {model_name!r}")
            if label in seen_names:
                raise ValueError(f"fleet.hosts.{host}: duplicate name {label!r}")
            if port in seen_ports:
                raise ValueError(f"fleet.hosts.{host}: duplicate port {port}")
            seen_names.add(label)
            seen_ports.add(port)


def config_for_host(
    host: str,
    *,
    models: list | None = None,
    cfg: Any = None,
) -> dict[str, Any]:
    """JSON for one host IP. Requires validate_fleet() already ran on cfg."""
    host = normalize_peer_ip(host)
    if not host or host not in cfg.fleet.hosts:
        raise UnknownHost(host)
    models = models if models is not None else load_model_list(cfg=cfg)
    by_name = {model.model_name: model for model in models}
    servers = []
    for i, raw in enumerate(cfg.fleet.hosts[host]):
        model_name, port, name = parse_host_row(raw, host, i)
        servers.append(
            Server.from_row(host, model_name, port, name, by_name[model_name]).box_json()
        )
    return {
        "host": host,
        "servers": servers,
    }


class Server:
    """One vLLM process from fleet.hosts (live health + box JSON)."""

    UNKNOWN = "unknown"
    STARTING = "starting"
    HEALTHY = "healthy"
    DOWN = "down"

    def __init__(
        self,
        *,
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
    def from_row(cls, host: str, model_name: str, port: int, name: str, model: Any) -> Server:
        vllm = model.model_info.vllm
        args = vllm.get("args") or []
        if isinstance(args, str):
            args = args.split()
        util = vllm.get("gpu_memory_utilization")
        maxlen = vllm.get("max_model_len")
        return cls(
            model_name=model_name,
            name=name,
            host=host,
            port=port,
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
        for host, rows in cfg.fleet.hosts.items():
            host = str(host).strip()
            for i, raw in enumerate(rows):
                model_name, port, name = parse_host_row(raw, host, i)
                servers.append(Server.from_row(host, model_name, port, name, by_name[model_name]))
        return cls(cfg, servers)

    def note_config_fetch(self, host: str, *, now: float | None = None) -> None:
        self.config_fetch_at[host] = now if now is not None else time.time()

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

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

"""vLLM host JSON: validate at process start, join models.yaml at request time.

GPU boxes fetch GET /vllm/config (no servers.yaml). Placement is fleet.hosts
keyed by client IP.

Terminology: **models.yaml** is admin definitions. A **model** is one row
(`model_name`). A **VllmServer** is one vLLM process. A **FleetDeployment**
is one intended LiteLLM backend (self-host public api_base or commercial
static api_base). The LiteLLM **Router** is the whole proxy. Box JSON
`servers[].model` is still the HF weights id.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from easydict import EasyDict as edict  # noqa: N813

from llmao.model_status import (
    ADMIN_LABELS,
    AWAITING,
    HEALTHY,
    STALLED,
    UNHEALTHY,
    DeploymentSnapshot,
    deployment_status,
    model_status,
    self_hosted_lifecycle,
)
from llmao.models import load_models, validate_models
from llmao.vast_client import fetch_port_map

_LOGGER = logging.getLogger(__name__)

_FLEET_NUMBERS = (
    "health_interval_s",
    "health_timeout_s",
    "health_grace_s",
    "health_fail_threshold",
    "skew_interval_s",
    "litellm_health_interval_s",
)


class UnknownHostError(KeyError):
    """No fleet.hosts entry for this client IP."""


def normalize_peer_ip(addr: str | None) -> str:
    ip = (addr or "").strip()
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return ip


# Box-claimed public IP (Vast PUBLIC_IPADDR). Not X-Forwarded-For.
HOST_HEADER = "X-LLMAO-Host"


def client_ip(
    *,
    remote_addr: str | None,
    forwarded_for: str | None,
    claimed_host: str | None = None,
) -> str:
    """Lookup key for fleet.hosts.

    If X-LLMAO-Host is present, it is the only key (Vast sits behind a
    transparent proxy; the TCP peer is not the instance). Otherwise leftmost
    X-Forwarded-For (one reverse proxy) or the peer. Werkzeug ProxyFix is WSGI
    and cannot wrap Quart asgi_app (ASGI).
    """
    claimed = normalize_peer_ip(claimed_host)
    if claimed:
        _LOGGER.info(
            "%s=%r remote_addr=%r X-Forwarded-For=%r",
            HOST_HEADER,
            claimed,
            remote_addr,
            forwarded_for,
        )
        return claimed
    if forwarded_for and forwarded_for.strip():
        chosen = normalize_peer_ip(forwarded_for.split(",")[0])
        _LOGGER.info("X-Forwarded-For=%r remote_addr=%r client_ip=%s", forwarded_for, remote_addr, chosen)
        return chosen
    return normalize_peer_ip(remote_addr)


def parse_host_row(raw: Any, host: str, index: int) -> tuple[str, int, str, int | None]:
    """[model, port] | [model, port, name] | [model, port, name, public_port]

    The fourth element pins the public port for providers whose mapping we
    cannot look up. Vast exposes one through its API; RunPod does not, so a
    RunPod box would otherwise never get an api_base and could never go
    healthy -- the model simply shows unavailable with nothing to say why.

    Use null for `name` to reach the fourth slot without renaming a server:

        - [qwen3.8-27b, 8004, null, 16643]
    """
    if not isinstance(raw, (list, tuple)) or len(raw) not in (2, 3, 4):
        raise ValueError(
            f"fleet.hosts.{host}[{index}] must be [model, port], "
            f"[model, port, name] or [model, port, name, public_port]"
        )
    model_name = str(raw[0]).strip()
    try:
        port = int(raw[1])
    except (TypeError, ValueError) as e:
        raise ValueError(f"fleet.hosts.{host}[{index}] port must be an int") from e
    name = str(raw[2]).strip() if len(raw) >= 3 and raw[2] is not None else model_name
    public_port = None
    if len(raw) == 4 and raw[3] is not None:
        try:
            public_port = int(raw[3])
        except (TypeError, ValueError) as e:
            raise ValueError(f"fleet.hosts.{host}[{index}] public_port must be an int") from e
    if not model_name or not name:
        raise ValueError(f"fleet.hosts.{host}[{index}] needs model and name")
    return model_name, port, name, public_port


def validate_fleet(cfg: Any, models: list | None = None) -> None:
    """Fail-fast if fleet.hosts or models.yaml cannot be joined. Call at startup."""
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
    if "vast" in cfg.fleet:
        try:
            key = str(cfg.fleet.vast.api_key or "").strip()
        except (AttributeError, KeyError) as e:
            raise ValueError("config.yaml: fleet.vast.api_key is required") from e
        if not key:
            raise ValueError("config.yaml: fleet.vast.api_key is required")

    models = models if models is not None else load_models(cfg=cfg)
    validate_models(models)
    known = {str(model.model_name) for model in models}
    by_name = {str(model.model_name): model for model in models}
    for host, rows in hosts.items():
        host = str(host).strip()
        if not host:
            raise ValueError("fleet.hosts has an empty IP key")
        if not isinstance(rows, (list, tuple)):
            raise ValueError(f"fleet.hosts.{host} must be a list of [model, port] rows")
        seen_names: set[str] = set()
        seen_ports: set[int] = set()
        for i, raw in enumerate(rows):
            model_name, port, label, _ = parse_host_row(raw, host, i)
            if model_name not in known:
                raise ValueError(f"fleet.hosts.{host}[{i}]: unknown model {model_name!r}")
            if not by_name[model_name].model_info.self_hosted:
                raise ValueError(f"fleet.hosts.{host}[{i}]: {model_name} is not self_hosted")
            if label in seen_names:
                raise ValueError(f"fleet.hosts.{host}: duplicate name {label!r}")
            if port in seen_ports:
                raise ValueError(f"fleet.hosts.{host}: duplicate port {port}")
            seen_names.add(label)
            seen_ports.add(port)


def _vllm_api_key(cfg: Any, host: str, listen_port: int) -> str:
    # Not a top-level import. vllm_api_key.py imports normalize_peer_ip from
    # this module. Importing derive_vllm_api_key up here would make each file
    # load the other before either finished initializing.
    #
    # cfg.fleet.vllm_api_salt is required. A missing attribute raises here.
    # Do not substitute "" — derive_vllm_api_key would then be the only check,
    # and a future edit could HMAC an empty salt and ship a public bearer.
    from llmao.vllm_api_key import derive_vllm_api_key

    return derive_vllm_api_key(cfg.fleet.vllm_api_salt, host, listen_port)


def config_for_host(
    host: str,
    *,
    models: list | None = None,
    cfg: Any = None,
) -> dict[str, Any]:
    """JSON for one host IP. Requires validate_fleet() already ran on cfg."""
    host = normalize_peer_ip(host)
    if not host or host not in cfg.fleet.hosts:
        raise UnknownHostError(host)
    models = models if models is not None else load_models(cfg=cfg)
    by_name = {model.model_name: model for model in models}
    servers = []
    for i, raw in enumerate(cfg.fleet.hosts[host]):
        # The box is told its listen port; the public port is ours, not its
        # business -- it binds inside the container.
        model_name, port, name, _ = parse_host_row(raw, host, i)
        servers.append(VllmServer.from_row(host, model_name, port, name, by_name[model_name], cfg).box_json())
    return {
        "host": host,
        "servers": servers,
    }


class VllmServer:
    """One vLLM process from fleet.hosts (live health + box JSON)."""

    def __init__(
        self,
        *,
        model_name: str,
        name: str,
        host: str,
        listen_port: int,
        hf_model: str,
        api_key: str,
        args: list[str],
        gpu_memory_utilization: float | None = None,
        max_model_len: int | None = None,
        vram_gb: float | None = None,
        disk_gb: float | None = None,
        public_port: int | None = None,
    ):
        self.model_name = model_name
        self.name = name
        self.host = host
        self.listen_port = int(listen_port)
        self.public_port = int(public_port) if public_port is not None else None
        # Set when the port came from the host row rather than a provider
        # API. apply_port_map leaves these alone -- otherwise a Vast lookup
        # that does not know this host would log a warning and, worse, a
        # future resolver could overwrite a correct value with a guess.
        self.public_port_pinned = False
        self.hf_model = hf_model
        self.api_key = api_key
        self.args = args
        self.gpu_memory_utilization = gpu_memory_utilization
        self.max_model_len = max_model_len
        # Declared requirements, served to the box so it can refuse before
        # pulling tens of GB of weights. NOT host capacity -- that is
        # discovered on the box from nvidia-smi, because a hand-typed figure
        # is wrong the first time a provider supplies a different card than
        # was ordered.
        self.vram_gb = vram_gb
        self.disk_gb = disk_gb
        # Observed from the box, not configured. None until the server has
        # been SERVING and a scrape has succeeded.
        #
        # kv_cache_tokens is the figure that decides whether max_model_len is
        # safe: a serving length above the measured cache produces HANGS, not
        # errors, which is the sharpest failure mode in the stack. It has only
        # ever been visible by reading a vLLM startup log by hand.
        self.kv_cache_tokens: int | None = None
        self.observed_max_model_len: int | None = None
        self.observed_at: float | None = None
        # Named by model_status. Awaiting contact until the box fetches its
        # config (config_served_at) and the probes run; see self_hosted_lifecycle.
        self.state = AWAITING
        self.config_served_at: float | None = None
        self.last_ok = None
        self.last_error = None
        self.fails = 0
        # The last probe's observation, kept so the roll-up can re-run the
        # lifecycle at the current time without reconstructing counters from
        # the state name. None until the first probe.
        self.last_probe_ok: bool | None = None
        self.vllm_probed_at: float | None = None
        self.reached: bool | None = None

    @classmethod
    def from_row(
        cls,
        host: str,
        model_name: str,
        port: int,
        name: str,
        model: Any,
        cfg: Any = None,
    ) -> VllmServer:
        vllm = model.model_info.vllm
        args = vllm.get("args") or []
        if isinstance(args, str):
            args = args.split()
        util = vllm.get("gpu_memory_utilization")
        maxlen = vllm.get("max_model_len")
        vram = vllm.get("vram_gb")
        disk = vllm.get("disk_gb")
        return cls(
            model_name=model_name,
            name=name,
            host=host,
            listen_port=port,
            hf_model=str(vllm.model),
            # Bearer for `vllm serve --api-key` and for LiteLLM at /model/new.
            # HMAC of fleet.vllm_api_salt plus this host and listen port.
            # Not fleet.key (boxes have that). Not a models.yaml field.
            # Not selfhost_api_key. Missing salt raises; an empty bearer
            # would start vLLM with no auth on a public port.
            api_key=_vllm_api_key(cfg, host, port),
            args=[str(a) for a in args],
            gpu_memory_utilization=float(util) if util is not None else None,
            max_model_len=int(maxlen) if maxlen is not None else None,
            vram_gb=float(vram) if vram is not None else None,
            disk_gb=float(disk) if disk is not None else None,
        )

    @property
    def root_url(self) -> str | None:
        if self.public_port is None:
            return None
        return f"http://{self.host}:{self.public_port}"

    @property
    def health_url(self) -> str | None:
        if self.root_url is None:
            return None
        return f"{self.root_url}/health"

    @property
    def api_base(self) -> str | None:
        if self.root_url is None:
            return None
        return f"{self.root_url}/v1"

    @property
    def oversized(self) -> bool:
        """Serving length exceeds the measured KV cache.

        This is the hang condition. vLLM accepts a --max-model-len above what
        the cache can hold and then stalls on a request that needs the space,
        rather than refusing at startup -- so it looks like a slow model, not
        a misconfiguration.

        False when either figure is unknown: an unscraped server is not a
        broken one.
        """
        if self.kv_cache_tokens is None:
            return False
        served = self.observed_max_model_len or self.max_model_len
        if served is None:
            return False
        return served > self.kv_cache_tokens

    def box_json(self) -> dict[str, Any]:
        """Wire payload for GET /vllm/config (servers[].model is the HF weights id)."""
        out: dict[str, Any] = {
            "name": self.name,
            "model": self.hf_model,
            "host": self.host,
            "port": self.listen_port,
            "api_key": self.api_key,
            "args": list(self.args),
        }
        if self.gpu_memory_utilization is not None:
            out["gpu_memory_utilization"] = self.gpu_memory_utilization
        if self.max_model_len is not None:
            out["max_model_len"] = self.max_model_len
        if self.vram_gb is not None:
            out["vram_gb"] = self.vram_gb
        if self.disk_gb is not None:
            out["disk_gb"] = self.disk_gb
        return out

    def record_probe(
        self,
        ok: bool,
        *,
        now: float,
        grace_s: float,
        fail_threshold: int,
        err: str | None = None,
        reached: bool | None = None,
        in_litellm: bool = False,
    ) -> None:
        was = self.state
        if ok:
            self.last_ok = now
            self.last_error = None
            self.fails = 0
        else:
            self.fails += 1
            self.last_error = err or "unhealthy"
        self.last_probe_ok = ok
        self.vllm_probed_at = now
        if reached is not None:
            self.reached = reached
        # The counters above are the observation. The name comes from one place.
        cfg = edict(health_grace_s=grace_s, health_fail_threshold=fail_threshold)
        snap = DeploymentSnapshot(
            model_name=self.model_name,
            self_hosted=True,
            probe_ok=ok,
            fails=self.fails,
            last_ok=self.last_ok,
            vllm_probed_at=now,
            in_litellm=in_litellm,
            config_served_at=self.config_served_at,
            reached=self.reached,
        )
        self.state = self_hosted_lifecycle(snap, cfg, now)
        if self.state == HEALTHY and was != HEALTHY:
            _LOGGER.info("fleet server %s@%s healthy", self.name, self.api_base)
        elif self.state in (UNHEALTHY, STALLED) and was not in (UNHEALTHY, STALLED):
            _LOGGER.warning("fleet server %s@%s %s (%s)", self.name, self.api_base, self.state, self.last_error)


class FleetDeployment:
    """One intended LiteLLM deployment (definitions + api_base).

    Self-hosted: wraps a VllmServer (public port may be unset). Commercial:
    static api_base from models.yaml. Skew/health live here, not on VllmServer.
    """

    def __init__(
        self,
        *,
        model_name: str,
        name: str,
        self_hosted: bool,
        api_base: str | None = None,
        vllm: VllmServer | None = None,
    ):
        self.model_name = model_name
        self.name = name
        self.self_hosted = self_hosted
        self._api_base = api_base
        self.vllm = vllm
        self.in_litellm = False
        self.litellm_healthy: bool | None = None
        self.litellm_health_at: float | None = None
        self.skew: list[str] = []
        # Set by the config skew pass when LiteLLM has this api_base under another model_name.
        self.config_mismatch = False

    @classmethod
    def from_vllm(cls, srv: VllmServer) -> FleetDeployment:
        return cls(
            model_name=srv.model_name,
            name=srv.name,
            self_hosted=True,
            vllm=srv,
        )

    @classmethod
    def from_commercial(cls, model: Any) -> FleetDeployment:
        params = model.litellm_params
        base = str(params.get("api_base") or "").strip()
        return cls(
            model_name=str(model.model_name),
            name=str(model.model_name),
            self_hosted=False,
            api_base=base or None,
        )

    @property
    def api_base(self) -> str | None:
        if self.vllm is not None:
            return self.vllm.api_base
        return self._api_base


@dataclass(frozen=True)
class ExtraLiteLLM:
    """One LiteLLM route that config.yaml does not list."""

    host: str
    port: int | None
    model_name: str
    skew: tuple[str, ...]


class Fleet:
    """Live fleet: vLLM servers, intended deployments, lifecycle probes."""

    def __init__(
        self,
        cfg: Any,
        servers: list[VllmServer],
        deployments: list[FleetDeployment] | None = None,
        models: dict[str, Any] | None = None,
        after_probe=None,
    ):
        self.cfg = cfg
        self.servers = servers
        self.deployments = deployments if deployments is not None else [FleetDeployment.from_vllm(s) for s in servers]
        # model_name → models.yaml row. POST /model/new copies litellm_params
        # from here; LiteLLM /model/info encrypts them on the way back.
        self.models = models if models is not None else {}
        # Awaitable after each vLLM probe tick (LiteLLMBackend.sync_selfhost).
        # Wired from create_app so this module does not import the LiteLLM client.
        self.after_probe = after_probe
        self.config_fetch_at: dict[str, float] = {}
        # Fleet-key GETs for IPs not in fleet.hosts. Process memory only.
        self.unknown_config_fetches: dict[str, dict[str, float | int]] = {}
        # Admin labels (Approved, Reboot requested), keyed by normalized host IP.
        # In-memory only: cleared on process restart, and they never affect the
        # computed lifecycle -- the page reads them for display. See admin_label.
        self.admin_labels: dict[str, str] = {}
        self.admin_label_at: dict[str, float] = {}
        # LiteLLM routes that match no deployment. Rebuilt each config skew pass.
        # Identity is host, port, and model name. api_base is not a primary key:
        # one host serves several ports, and one host:port can report more than one model.
        self.extra_litellm: list[ExtraLiteLLM] = []

    @classmethod
    def from_cfg(cls, cfg: Any, models: list | None = None) -> Fleet:
        models = models if models is not None else load_models(cfg=cfg)
        by_name = {model.model_name: model for model in models}
        local = "vast" not in cfg.fleet
        servers = []
        for host, rows in cfg.fleet.hosts.items():
            host = str(host).strip()
            for i, raw in enumerate(rows):
                model_name, port, name, public = parse_host_row(raw, host, i)
                srv = VllmServer.from_row(host, model_name, port, name, by_name[model_name], cfg)
                if public is not None:
                    # Pinned in config: no provider lookup can override it.
                    srv.public_port = public
                    srv.public_port_pinned = True
                elif local:
                    srv.public_port = srv.listen_port
                servers.append(srv)
        deployments = [FleetDeployment.from_vllm(s) for s in servers]
        for model in models:
            if not model.model_info.self_hosted:
                deployments.append(FleetDeployment.from_commercial(model))
        return cls(cfg, servers, deployments, models=by_name)

    def note_config_fetch(self, host: str, *, now: float | None = None) -> None:
        """Record that we handed this box its config.

        Stamps ``config_served_at`` on every server for the host, which is what
        moves them from awaiting contact into the Loading window (the next
        probe, or immediately for any probe running now, re-derives the state).
        """
        stamp = now if now is not None else time.time()
        self.config_fetch_at[host] = stamp
        for srv in self.servers:
            if srv.host == host:
                srv.config_served_at = stamp
        self.unknown_config_fetches.pop(normalize_peer_ip(host), None)

    def set_admin_label(self, host: str, label: str | None, *, now: float | None = None) -> None:
        """Set, change, or clear a host's current admin label.

        ``label`` is ``APPROVED`` / ``REBOOT_REQUESTED``, or ``None`` to clear.
        The value is display-only: it is never read by the lifecycle, and it is
        lost when the process restarts (see the restart-recovery notes in
        docs/fleet-state.md). One label per host -- these are phases of one
        onboarding, so a host is in at most one at a time.
        """
        if label is not None and label not in ADMIN_LABELS:
            raise ValueError(f"Unknown admin label: {label!r}")
        host = normalize_peer_ip(host)
        if label is None:
            self.admin_labels.pop(host, None)
            self.admin_label_at.pop(host, None)
            return
        stamp = now if now is not None else time.time()
        self.admin_labels[host] = label
        self.admin_label_at[host] = stamp

    def admin_label(self, host: str) -> tuple[str | None, float | None]:
        """The host's current admin label and when it was set, else (None, None)."""
        host = normalize_peer_ip(host)
        label = self.admin_labels.get(host)
        return label, self.admin_label_at.get(host) if label is not None else None

    def note_unknown_config_fetch(self, host: str, *, now: float | None = None) -> None:
        """Remember a fleet-key config fetch for an IP that is not in fleet.hosts.

        Membership comes from config already checked by validate_fleet. Cap is
        50, dropping the oldest last-seen. A member IP is removed and not recorded.
        """
        host = normalize_peer_ip(host)
        stamp = time.time() if now is None else now
        members = {normalize_peer_ip(str(ip)) for ip in self.cfg.fleet.hosts}
        for known in list(self.unknown_config_fetches):
            if known in members:
                del self.unknown_config_fetches[known]
        if host in members:
            return
        rec = self.unknown_config_fetches.get(host)
        if rec is None:
            self.unknown_config_fetches[host] = {"first_seen": stamp, "last_seen": stamp, "count": 1}
        else:
            rec["last_seen"] = stamp
            rec["count"] = int(rec["count"]) + 1
        extra = len(self.unknown_config_fetches) - 50
        if extra > 0:
            oldest = sorted(
                self.unknown_config_fetches,
                key=lambda ip: float(self.unknown_config_fetches[ip]["last_seen"]),
            )
            for ip in oldest[:extra]:
                del self.unknown_config_fetches[ip]

    def apply_port_map(self, mapping: Any) -> None:
        """Set public_port from Vast IP → listen → HostPort. Leave unset if missing.

        Servers with a pinned public_port are skipped: the host row is more
        authoritative than a provider lookup that may not cover the host at
        all.
        """
        for srv in self.servers:
            if srv.public_port_pinned:
                continue
            by_listen = mapping.get(srv.host) if mapping is not None else None
            if not by_listen:
                _LOGGER.info("vast: no instance for fleet host %s", srv.host)
                continue
            public = by_listen.get(str(srv.listen_port))
            if public is None:
                _LOGGER.warning("vast: no HostPort for %s@%s listen %s", srv.name, srv.host, srv.listen_port)
                continue
            public = int(public)
            if srv.public_port != public:
                _LOGGER.info("vast: %s@%s listen %s public %s", srv.name, srv.host, srv.listen_port, public)
                srv.public_port = public

    def model_rollup(self, model_name: str, now: float | None = None):
        """Available / Degraded / Unavailable. validate_fleet already required the intervals."""
        stamp = time.time() if now is None else now
        cfg = self.cfg.fleet
        views = [deployment_status(snapshot_for_status(dep, cfg, stamp), cfg, stamp) for dep in self.deployments]
        catalog = True
        model = self.models.get(model_name) if self.models else None
        if model is not None:
            info = getattr(model, "model_info", None)
            flag = getattr(info, "self_hosted", None) if info is not None else None
            if flag is not None:
                catalog = bool(flag)
        return model_status(model_name, views, catalog_self_hosted=catalog)

    def model_in_litellm(self, model_name: str) -> bool:
        """True if any vLLM for this model_name has a LiteLLM deployment (skew)."""
        return any(d.model_name == model_name and d.in_litellm for d in self.deployments)

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
            in_litellm_by_srv = {dep.vllm: dep.in_litellm for dep in self.deployments if dep.vllm is not None}
            for srv in self.servers:
                url = srv.health_url
                if url is None:
                    continue
                was = srv.state
                ok, reached, err = await _get_health(client, url)
                srv.record_probe(
                    ok,
                    now=stamp,
                    grace_s=grace,
                    fail_threshold=threshold,
                    err=err,
                    reached=reached,
                    in_litellm=bool(in_litellm_by_srv.get(srv, False)),
                )
                # Scrape on the edge into SERVING, and once more if an earlier
                # attempt came back empty. Both values are fixed at engine
                # init, so re-reading them every probe would be waste.
                if srv.state == HEALTHY and (was != HEALTHY or srv.kv_cache_tokens is None):
                    root = srv.root_url
                    if root:
                        kv, mml = await fetch_observed(client, root, srv.api_key)
                        if kv is not None:
                            srv.kv_cache_tokens = kv
                        if mml is not None:
                            srv.observed_max_model_len = mml
                        if kv is not None or mml is not None:
                            srv.observed_at = stamp
        finally:
            if own:
                await client.aclose()

    async def refresh_public_ports(self, client: httpx.AsyncClient) -> None:
        if "vast" not in self.cfg.fleet:
            return
        mapping = await fetch_port_map(self.cfg.fleet.vast.api_key, client=client)
        self.apply_port_map(mapping)

    async def run_lifecycle(self) -> None:
        interval = float(self.cfg.fleet.health_interval_s)
        timeout = float(self.cfg.fleet.health_timeout_s)
        while True:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
                    try:
                        await self.refresh_public_ports(client)
                    except Exception:
                        _LOGGER.exception("vast port map failed")
                    await self.probe_all(client=client)
                    if self.after_probe is not None:
                        await self.after_probe()
            except Exception:
                _LOGGER.exception("fleet lifecycle probe failed")
            await asyncio.sleep(interval)


def snapshot_for_status(dep, cfg: edict, now: float) -> DeploymentSnapshot:
    """The last stored probe observation for this deployment.

    record_probe keeps the counters (last_probe_ok, fails, last_ok,
    vllm_probed_at) and config_served_at on the server, so deployment_status
    can re-run the same lifecycle function the probe loop uses -- at the
    current time, so a Loading deployment that has since outlasted the grace
    window reads Stalled without waiting for the next probe. cfg.fleet is the
    validated interval block.
    """
    litellm_at = now if dep.litellm_healthy is not None else None
    mismatch = bool(dep.config_mismatch)
    srv = dep.vllm
    if srv is None:
        return DeploymentSnapshot(
            model_name=dep.model_name,
            self_hosted=False,
            in_litellm=dep.in_litellm,
            litellm_healthy=dep.litellm_healthy,
            litellm_health_at=litellm_at,
            config_mismatch=mismatch,
        )
    return DeploymentSnapshot(
        model_name=dep.model_name,
        self_hosted=True,
        probe_ok=srv.last_probe_ok,
        fails=srv.fails,
        last_ok=srv.last_ok,
        vllm_probed_at=srv.vllm_probed_at,
        in_litellm=dep.in_litellm,
        litellm_healthy=dep.litellm_healthy,
        litellm_health_at=litellm_at,
        context_window=srv.observed_max_model_len or srv.max_model_len,
        kv_cache_tokens=srv.kv_cache_tokens,
        config_mismatch=mismatch,
        config_served_at=srv.config_served_at,
        reached=srv.reached,
    )


def add_refusal(dep) -> str | None:
    """Why the Fleet Add button must not call add_deployment.

    None means the row is self-hosted, not already in LiteLLM, and vLLM is
    SERVING. add_deployment itself does not apply this gate.
    """
    if not dep.self_hosted or dep.vllm is None:
        return "not a self-hosted deployment"
    if dep.in_litellm:
        return "already in LiteLLM"
    if dep.vllm.state != HEALTHY:
        return "vLLM is not serving yet"
    return None


def parse_kv_cache_tokens(metrics_text: str) -> int | None:
    """KV cache size in tokens from vLLM's Prometheus output, or None.

    vLLM reports cache geometry as labels on an Info-style metric:

        vllm:cache_config_info{block_size="16",num_gpu_blocks="33960",
                               kv_cache_size_tokens="855836",...} 1.0

    Prefer kv_cache_size_tokens, which is the same number vLLM prints at
    startup as "GPU KV cache size: N tokens".

    Do NOT compute it as num_gpu_blocks * block_size. On the A100 serving
    Gemma those give 543,360 against a reported 855,836 -- blocks do not map
    uniformly to tokens for a model with heterogeneous head dimensions, and
    the product understates by a third. Understating the cache would falsely
    flag a correctly-sized server as oversized.

    The product is kept only as a fallback for builds that do not emit the
    token count, where an approximate figure beats none.

    Returns None rather than raising when the metric is absent or shaped
    differently -- vLLM's metric names are not a stable API, and a missing
    figure should degrade the display rather than break the probe loop.
    """
    for line in metrics_text.splitlines():
        if not line.startswith("vllm:cache_config_info"):
            continue
        start, end = line.find("{"), line.rfind("}")
        if start < 0 or end < start:
            continue
        labels: dict[str, str] = {}
        for part in line[start + 1 : end].split(","):
            k, _, v = part.partition("=")
            labels[k.strip()] = v.strip().strip('"')

        direct = labels.get("kv_cache_size_tokens")
        if direct and direct != "None":
            try:
                tokens = int(direct)
            except ValueError:
                tokens = 0
            if tokens > 0:
                return tokens

        try:
            blocks = int(labels["num_gpu_blocks"])
            size = int(labels["block_size"])
        except (KeyError, ValueError):
            continue
        if blocks > 0 and size > 0:
            return blocks * size
    return None


def parse_max_model_len(models_json: Any) -> int | None:
    """Served context window from /v1/models, or None."""
    try:
        rows = models_json["data"]
    except (TypeError, KeyError):
        return None
    for row in rows or []:
        value = row.get("max_model_len") if isinstance(row, dict) else None
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


async def fetch_observed(client: httpx.AsyncClient, base: str, api_key: str) -> tuple[int | None, int | None]:
    """Scrape (kv_cache_tokens, max_model_len) from a serving vLLM.

    base is the server root, not api_base: /metrics sits at the root and the
    model list at /v1/models, so a /v1-suffixed base would 404 the model list
    and leave both fields unknown.

    Both are fixed at engine init, so this is called on the transition into
    SERVING rather than on every probe -- polling an unchanging value every
    45s would be waste.

    /metrics is unauthenticated on vLLM; /v1/models is not, so the key goes on
    both rather than reasoning about which needs it.

    Never raises: an unreachable or differently-shaped endpoint leaves the
    fields None and the UX shows them as unknown.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    kv = mml = None
    try:
        resp = await client.get(f"{base}/metrics", headers=headers)
        if resp.status_code == 200:
            kv = parse_kv_cache_tokens(resp.text)
    except httpx.HTTPError:
        pass
    try:
        resp = await client.get(f"{base}/v1/models", headers=headers)
        if resp.status_code == 200:
            mml = parse_max_model_len(resp.json())
    except (httpx.HTTPError, ValueError):
        pass
    return kv, mml


async def _get_health(client: httpx.AsyncClient, url: str) -> tuple[bool, bool, str | None]:
    """(ok, reached, err). ``reached`` is True when vLLM's HTTP server answered
    at all (e.g. a 503 while the model is still loading); False for no
    connection. ``ok`` is only a 200.
    """
    try:
        resp = await client.get(url)
    except httpx.HTTPError as e:
        return False, False, str(e)
    if resp.status_code == 200:
        return True, True, None
    return False, True, f"HTTP {resp.status_code}"

"""vLLM host JSON: validate at process start, join catalog at request time.

GPU boxes fetch GET /vllm/config (no servers.yaml). Placement is fleet.hosts
keyed by client IP.

Terminology: the **catalog** is model_list.yaml. A **model** is one catalog
recipe (`model_name`). A **VllmServer** is one vLLM process. A
**FleetDeployment** is one intended LiteLLM backend (self-host public
api_base or commercial static api_base). The LiteLLM **Router** is the whole
proxy. Box JSON `servers[].model` is still the HF weights id.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from llmao.models import load_model_list, validate_catalog
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


class UnknownHost(KeyError):
    """No fleet.hosts entry for this client IP."""


def normalize_peer_ip(addr: str | None) -> str:
    ip = (addr or "").strip()
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return ip


def client_ip(*, remote_addr: str | None, forwarded_for: str | None) -> str:
    """Box public IP: leftmost X-Forwarded-For (one reverse proxy), else peer.

    Werkzeug ProxyFix is WSGI and cannot wrap Quart asgi_app (ASGI).
    """
    if forwarded_for and forwarded_for.strip():
        chosen = normalize_peer_ip(forwarded_for.split(",")[0])
        _LOGGER.info(
            f"X-Forwarded-For={forwarded_for!r} remote_addr={remote_addr!r} client_ip={chosen}"
        )
        return chosen
    return normalize_peer_ip(remote_addr)


def parse_host_row(
    raw: Any, host: str, index: int
) -> tuple[str, int, str, int | None]:
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
            raise ValueError(
                f"fleet.hosts.{host}[{index}] public_port must be an int"
            ) from e
    if not model_name or not name:
        raise ValueError(f"fleet.hosts.{host}[{index}] needs model and name")
    return model_name, port, name, public_port


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
    if "vast" in cfg.fleet:
        try:
            key = str(cfg.fleet.vast.api_key or "").strip()
        except (AttributeError, KeyError) as e:
            raise ValueError("config.yaml: fleet.vast.api_key is required") from e
        if not key:
            raise ValueError("config.yaml: fleet.vast.api_key is required")

    models = models if models is not None else load_model_list(cfg=cfg)
    validate_catalog(models)
    catalog = {str(model.model_name) for model in models}
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
            if model_name not in catalog:
                raise ValueError(f"fleet.hosts.{host}[{i}]: unknown model {model_name!r}")
            if not by_name[model_name].model_info.self_hosted:
                raise ValueError(
                    f"fleet.hosts.{host}[{i}]: {model_name} is not self_hosted"
                )
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
        # The box is told its listen port; the public port is ours, not its
        # business -- it binds inside the container.
        model_name, port, name, _ = parse_host_row(raw, host, i)
        servers.append(
            VllmServer.from_row(
                host, model_name, port, name, by_name[model_name], cfg
            ).box_json()
        )
    return {
        "host": host,
        "servers": servers,
    }


class VllmServer:
    """One vLLM process from fleet.hosts (live health + box JSON)."""

    PENDING = "pending"
    STARTING = "starting"
    SERVING = "serving"
    DOWN = "down"

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
        self.state = self.PENDING
        self.seen_at = time.time()
        self.last_ok = None
        self.last_error = None
        self.fails = 0

    @classmethod
    def from_row(
        cls, host: str, model_name: str, port: int, name: str, model: Any,
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
            # Bearer token the box passes to `vllm serve --api-key`, and the
            # same value LiteLLM presents when calling that server.
            #
            # A catalog entry may still carry one (older model_list.yaml did),
            # but it is not a property of the model -- it is a credential for
            # one machine. The fleet-wide value in config is the current
            # source; per-instance keys generated at host-add time are the
            # eventual one.
            #
            # An empty result means vLLM starts UNAUTHENTICATED on a public
            # port, so this must resolve to something.
            api_key=str(
                model.litellm_params.get("api_key")
                or (cfg.fleet.get("selfhost_api_key") if cfg is not None else "")
                or ""
            ),
            args=[str(a) for a in args],
            gpu_memory_utilization=float(util) if util is not None else None,
            max_model_len=int(maxlen) if maxlen is not None else None,
            vram_gb=float(vram) if vram is not None else None,
            disk_gb=float(disk) if disk is not None else None,
        )

    @property
    def health_url(self) -> str | None:
        if self.public_port is None:
            return None
        return f"http://{self.host}:{self.public_port}/health"

    @property
    def api_base(self) -> str | None:
        if self.public_port is None:
            return None
        return f"http://{self.host}:{self.public_port}/v1"

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
    ) -> None:
        if ok:
            if self.state != self.SERVING:
                _LOGGER.info(f"fleet server {self.name}@{self.api_base} serving")
            self.state = self.SERVING
            self.last_ok = now
            self.last_error = None
            self.fails = 0
            return
        self.fails += 1
        self.last_error = err or "unhealthy"
        if self.state == self.SERVING:
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


class FleetDeployment:
    """One intended LiteLLM deployment (catalog + api_base).

    Self-hosted: wraps a VllmServer (public port may be unset). Commercial:
    static api_base from the catalog. Skew/health live here, not on VllmServer.
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


class Fleet:
    """Live fleet: vLLM servers, intended deployments, lifecycle probes."""

    BADGE_UP = "up"
    BADGE_STARTING = "starting"
    BADGE_DOWN = "down"
    BADGE_MIXED = "mixed"

    def __init__(
        self,
        cfg: Any,
        servers: list[VllmServer],
        deployments: list[FleetDeployment] | None = None,
        catalog: dict[str, Any] | None = None,
        after_probe=None,
    ):
        self.cfg = cfg
        self.servers = servers
        self.deployments = deployments if deployments is not None else [
            FleetDeployment.from_vllm(s) for s in servers
        ]
        # model_name → catalog row. POST /model/new copies litellm_params from
        # here; LiteLLM /model/info encrypts them on the way back.
        self.catalog = catalog if catalog is not None else {}
        # Awaitable after each vLLM probe tick (LiteLLMBackend.sync_selfhost).
        # Wired from create_app so this module does not import the LiteLLM client.
        self.after_probe = after_probe
        self.config_fetch_at: dict[str, float] = {}

    @classmethod
    def from_cfg(cls, cfg: Any, models: list | None = None) -> Fleet:
        models = models if models is not None else load_model_list(cfg=cfg)
        by_name = {model.model_name: model for model in models}
        local = "vast" not in cfg.fleet
        servers = []
        for host, rows in cfg.fleet.hosts.items():
            host = str(host).strip()
            for i, raw in enumerate(rows):
                model_name, port, name, public = parse_host_row(raw, host, i)
                srv = VllmServer.from_row(
                    host, model_name, port, name, by_name[model_name], cfg
                )
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
        return cls(cfg, servers, deployments, catalog=by_name)

    def note_config_fetch(self, host: str, *, now: float | None = None) -> None:
        self.config_fetch_at[host] = now if now is not None else time.time()

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
                _LOGGER.info(f"vast: no instance for fleet host {srv.host}")
                continue
            public = by_listen.get(str(srv.listen_port))
            if public is None:
                _LOGGER.warning(
                    f"vast: no HostPort for {srv.name}@{srv.host} listen {srv.listen_port}"
                )
                continue
            public = int(public)
            if srv.public_port != public:
                _LOGGER.info(
                    f"vast: {srv.name}@{srv.host} listen {srv.listen_port} public {public}"
                )
                srv.public_port = public

    def model_health(self, model_name: str) -> str:
        """Aggregate: up / starting / down / mixed, or empty if no servers."""
        states = [s.state for s in self.servers if s.model_name == model_name]
        if not states:
            return ""
        uniq = set(states)
        if uniq == {VllmServer.SERVING}:
            return self.BADGE_UP
        if uniq <= {VllmServer.STARTING, VllmServer.PENDING}:
            return self.BADGE_STARTING
        if uniq == {VllmServer.DOWN}:
            return self.BADGE_DOWN
        if VllmServer.SERVING in uniq and uniq <= {VllmServer.SERVING, VllmServer.STARTING, VllmServer.PENDING}:
            return self.BADGE_UP
        return self.BADGE_MIXED

    def model_in_litellm(self, model_name: str) -> bool:
        """True if any vLLM for this catalog id has a LiteLLM deployment (skew)."""
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
            for srv in self.servers:
                url = srv.health_url
                if url is None:
                    continue
                was = srv.state
                ok, err = await _get_health(client, url)
                srv.record_probe(
                    ok, now=stamp, grace_s=grace, fail_threshold=threshold, err=err
                )
                # Scrape on the edge into SERVING, and once more if an earlier
                # attempt came back empty. Both values are fixed at engine
                # init, so re-reading them every probe would be waste.
                if srv.state == srv.SERVING and (
                    was != srv.SERVING or srv.kv_cache_tokens is None
                ):
                    base = srv.api_base
                    if base:
                        kv, mml = await fetch_observed(client, base, srv.api_key)
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
        for part in line[start + 1:end].split(","):
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


async def fetch_observed(
    client: httpx.AsyncClient, base: str, api_key: str
) -> tuple[int | None, int | None]:
    """Scrape (kv_cache_tokens, max_model_len) from a serving vLLM.

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


async def _get_health(client: httpx.AsyncClient, url: str) -> tuple[bool, str | None]:
    try:
        resp = await client.get(url)
    except httpx.HTTPError as e:
        return False, str(e)
    if resp.status_code == 200:
        return True, None
    return False, f"HTTP {resp.status_code}"

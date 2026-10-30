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
import ipaddress
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from easydict import EasyDict as edict  # noqa: N813

from llmao.intent import (
    LEGACY_SERVERS,
    VLLM_LISTEN_PORTS,
    Approval,
    IntendedDeployment,
    gpu_host_api_key,
    host_intents,
    intended_deployments,
    parse_host_row,
    validate_providers,
    vllm_api_key,
)
from llmao.model_status import (
    AWAITING,
    FOREIGN,
    HEALTHY,
    REJECTED,
    SKEW_PHRASE,
    STALLED,
    UNHEALTHY,
    DeploymentSnapshot,
    deployment_status,
    model_status,
    self_hosted_lifecycle,
)
from llmao.models import VllmRecipe, validate_models, warn_deprecated
from llmao.provisioning import ConfigFetch, ProvisioningRecord
from llmao.reconcile import DeploymentKey, deployment_key, norm_base, normalize_peer_ip
from llmao.rental import ProviderRentals, Rental, match_rental
from llmao.runpod_client import fetch_pods, rentals_from_pods
from llmao.vast_client import fetch_instances, rentals_from_body

if TYPE_CHECKING:
    from llmao.proxy.deployments import LiteLLMDeployment, LiteLLMDeployments, LiteLLMHealth, LiteLLMHealthReport

_LOGGER = logging.getLogger(__name__)

_FLEET_NUMBERS = (
    "health_interval_s",
    "health_timeout_s",
    "health_grace_s",
    "health_fail_threshold",
    "skew_interval_s",
    "litellm_health_interval_s",
)


def _ago(ts: float | None) -> str:
    if ts is None:
        return "never"
    sec = max(0, int(time.time() - ts))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h"


def _is_ip_literal(name: str) -> bool:
    """Whether a server's name is itself an address (a legacy or local box), so it can be dialed as given."""
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


# How often RunPod's list of our pods is read. Placing a box is not urgent: a
# new pod is still provisioning when the next read comes round.
RUNPOD_LISTING_INTERVAL_S = 600

# Two hysteresis counts, one per clock. Each is a constant rather than a config
# key: no p6 change, no eyaml round-trip. Both mirror fleet.health_fail_threshold,
# which exists for the same reason: one failed vLLM probe does not mean a box
# is down, and one absence does not mean a thing is gone.
#
# Consecutive provider listings that must omit a host before llmao calls it
# unlisted (the listing clock). Not one: a RunPod pod is absent from the pod
# list while it provisions -- measured at over 90 minutes after a recreate --
# and a single absence means "not yet" as often as "gone". The count does not
# have to outlast that gap: unlisted is an attention verdict, a serving box
# keeps its route whatever the listing says, and a box that is still coming
# up stays quiet (docs/fleet-reconcile-design.md). Today Vast is read on the
# probe pass and RunPod every RUNPOD_LISTING_INTERVAL_S; the design moves both
# to one listing interval of about a minute, so three passes is a few minutes.
UNLISTED_PASSES = 3

# Consecutive reads of the LiteLLM deployments (skew_interval_s, 180s) on
# which a deployment no intent claims must persist before the attention strip
# shows it (the skew clock). Absence has innocent causes -- a provider lookup
# that failed this pass, a box mid-recreate, a deployment llmao is about to
# claim -- so the item waits this many passes. It is never deleted unattended:
# something instructed LiteLLM to serve it, and a human decides.
ORPHAN_PASSES = 3


def _listen_port_allowed(host: str, port: int) -> bool:
    return port in VLLM_LISTEN_PORTS or (normalize_peer_ip(host), port) in LEGACY_SERVERS


def _set_public_port(srv: VllmServer, by_listen: Any, source: str) -> None:
    """Set srv.public_port from a listen port → public port map; said when it changes.

    ``by_listen`` is keyed by the listen port as a string, which is how both
    providers' maps are built. A map that lacks this server's listen port is
    a warning: the listing knows the box but not this port, so the row or
    the box is wrong. A listen port llmao does not allow is left alone.
    """
    if not _listen_port_allowed(srv.host, srv.listen_port):
        return
    public = by_listen.get(str(srv.listen_port))
    if public is None:
        _LOGGER.warning("%s: no public port for %s@%s listen %s", source, srv.name, srv.host, srv.listen_port)
        return
    public = int(public)
    if srv.public_port != public:
        _LOGGER.info("%s: %s@%s listen %s public %s", source, srv.name, srv.host, srv.listen_port, public)
        srv.public_port = public


def _require_listen_port(host: str, port: int) -> None:
    if _listen_port_allowed(host, port):
        return
    allowed = ", ".join(str(p) for p in VLLM_LISTEN_PORTS)
    raise ValueError(f"{host}: listen port {port} is not one of {allowed}")


def validate_fleet(cfg: Any, *, models: list) -> None:
    """Fail-fast if fleet.hosts or models.yaml cannot be joined. Call at startup, with the loaded models."""
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
    # The bearer salt is checked here, once; every mint after this trusts it
    # (intent.derive_vllm_api_key). An empty or placeholder salt would HMAC
    # to a public bearer and start vLLM with no auth on a public port.
    salt = cfg.fleet.get("vllm_api_salt")
    if not isinstance(salt, str) or not salt.strip() or salt.strip().startswith("CHANGE_ME"):
        raise ValueError("config.yaml: fleet.vllm_api_salt is missing or CHANGE_ME")
    # The fleet key likewise: provisioning.fetch_refusal compares against it
    # on every config fetch and trusts that it is set.
    key = cfg.fleet.get("key")
    if not isinstance(key, str) or not key.strip() or key.strip().startswith("CHANGE_ME"):
        raise ValueError("config.yaml: fleet.key is missing or CHANGE_ME")
    validate_models(models)
    warn_deprecated(models)
    validate_providers(cfg, models)
    known = {str(model.model_name) for model in models}
    by_name = {str(model.model_name): model for model in models}
    for host, rows in hosts.items():
        host = str(host).strip()
        if not host:
            raise ValueError("fleet.hosts has an empty IP key")
        if not isinstance(rows, (list, tuple)):
            raise ValueError(f"fleet.hosts.{host} must be a list of [model, port] rows")
        if len(rows) > len(VLLM_LISTEN_PORTS):
            raise ValueError(f"fleet.hosts.{host}: at most {len(VLLM_LISTEN_PORTS)} servers")
        seen_names: set[str] = set()
        seen_ports: set[int] = set()
        legacy = False
        for i, raw in enumerate(rows):
            model_name, port, label, _ = parse_host_row(raw, host, i)
            _require_listen_port(host, port)
            if model_name not in known:
                raise ValueError(f"fleet.hosts.{host}[{i}]: unknown model {model_name!r}")
            if VllmRecipe.from_model(by_name[model_name]) is None:
                raise ValueError(f"fleet.hosts.{host}[{i}]: {model_name} has no vLLM recipe")
            if label in seen_names:
                raise ValueError(f"fleet.hosts.{host}: duplicate name {label!r}")
            if port in seen_ports:
                raise ValueError(f"fleet.hosts.{host}: duplicate port {port}")
            seen_names.add(label)
            seen_ports.add(port)
            if (normalize_peer_ip(host), port) in LEGACY_SERVERS:
                legacy = True
        if legacy:
            shared = str(getattr(cfg.fleet, "selfhost_api_key", "") or "").strip()
            if not shared or shared.startswith("CHANGE_ME"):
                raise ValueError(f"config.yaml: fleet.selfhost_api_key is required while {host} is still a legacy box")


class VllmServer:
    """One vLLM process we observe, built from its IntendedDeployment (live health)."""

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
        served_name: str | None = None,
        kv_bytes_per_token: int | None = None,
    ):
        self.model_name = model_name
        self.name = name
        # What vLLM answers to (--served-model-name), and so what LiteLLM must
        # ask for. Not `name`, which only labels this server.
        self.served_name = served_name or model_name
        # KV cache per token of context, from the model's architecture. With
        # max_model_len it is memory the box needs before serving anything;
        # sent so the box's fit check can count it. None: not declared.
        self.kv_bytes_per_token = kv_bytes_per_token
        # The server's name: its fleet.hosts key, what the box sends as its
        # host, what the bearer is minted from. A provider's id for the
        # machine (a Vast instance id, a RunPod pod id), or an address for
        # a legacy or local box; llmao does not care which.
        self.host = host
        # Where it is dialed, or None while unknown. A name that is an IP
        # literal is its own address; any other name has none until the
        # provider's listing places the server on a Rental with an address
        # (apply_rentals). Never the name: a pod id is not dialable.
        self.address: str | None = host if _is_ip_literal(host) else None
        self.listen_port = int(listen_port)
        # The port this server is dialed on: from the listing of the
        # provider that places it (Vast's HostPort, RunPod's portMappings),
        # applied on every lifecycle pass. None until a listing names the
        # box, unless a deprecated fourth host-row element seeds it, or the
        # fleet is local and public is listen.
        self.public_port = int(public_port) if public_port is not None else None
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
        # Observed from the box, not configured. None until read: the KV
        # cache size by the sampler from /metrics, the served window (and
        # observed_at) by the fleet from /v1/models once SERVING.
        #
        # kv_cache_tokens is the figure that decides whether max_model_len is
        # safe: a serving length above the measured cache produces HANGS, not
        # errors, which is the sharpest failure mode in the stack. It has only
        # ever been visible by reading a vLLM startup log by hand.
        self.kv_cache_tokens: int | None = None
        # The machine this server runs on, as its provider reports it
        # (llmao/rental.py). None until a provider's set names this host,
        # which for a machine no provider lists is never. Once matched, the
        # same machine's Rental keeps it here through a stop or a restart
        # under a new address.
        self.rental: Rental | None = None
        # True when every provider we rent from has listed its instances and
        # none has this host, for UNLISTED_PASSES passes running: the rental
        # has most likely ended. See Fleet.place_servers.
        self.unlisted = False
        # Consecutive listings that have omitted this host. Reset by any
        # sighting, so a host that flaps never reaches the threshold.
        self.unlisted_passes = 0
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
        # The first probe attempt in this process: the grace clock for a server
        # with no loading start that never answers (see self_hosted_lifecycle).
        self.first_probed_at: float | None = None
        # The first probe in this process that vLLM answered (a 503 counts):
        # the other loading start, beside config_served_at.
        self.first_reached_at: float | None = None
        # The last /v1/models answer with the working key (True: 401) and how
        # many passes running it has been refused. Rejected and Foreign.
        self.key_rejected: bool | None = None
        self.rejected_passes = 0

    @classmethod
    def from_intended(cls, intended: IntendedDeployment) -> VllmServer:
        """The observed record for one intended vLLM process; its public port comes later, from a listing."""
        recipe = intended.recipe
        return cls(
            model_name=intended.model_name,
            name=intended.name,
            host=intended.host,
            listen_port=intended.listen_port,
            hf_model=recipe.hf_model,
            # Minted with the intent (intent.vllm_api_key): what the box runs
            # with, what /v1/models is asked with, what LiteLLM registers.
            api_key=intended.api_key,
            args=list(recipe.args),
            gpu_memory_utilization=recipe.gpu_memory_utilization,
            max_model_len=recipe.max_model_len,
            vram_gb=recipe.vram_gb,
            disk_gb=recipe.disk_gb,
            served_name=recipe.served_name,
            kv_bytes_per_token=recipe.kv_bytes_per_token,
        )

    @property
    def last_ok_display(self) -> str:
        """Human-readable 'last_ok' timestamp."""
        return _ago(self.last_ok)

    @property
    def probe_display(self) -> str:
        """Last probe attempt, including a failure. Empty when never probed."""
        if self.vllm_probed_at is None:
            return ""
        age = _ago(self.vllm_probed_at)
        if self.last_probe_ok:
            return f"probed {age}"
        if self.reached is False:
            return f"probed {age}, no connection"
        if self.last_error:
            return f"probed {age}, {self.last_error}"
        return f"probed {age}"

    @property
    def key(self) -> DeploymentKey:
        """This server's identity everywhere: its listen URL and model name. Never the public port."""
        return deployment_key(self.model_name, host=self.host, listen_port=self.listen_port)

    @property
    def root_url(self) -> str | None:
        if self.public_port is None or self.address is None:
            return None
        return f"http://{self.address}:{self.public_port}"

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

    def same_deployment(self, host: str, port: int | None, model_name: str) -> bool:
        """True when this LiteLLM deployment is this server, reachable: on its public port."""
        if port is None or port != self.public_port or model_name != self.model_name:
            return False
        return self.address is not None and normalize_peer_ip(host) == self.address

    def on_listen_port(self, host: str, port: int | None, model_name: str) -> bool:
        """True when this LiteLLM deployment is this server, but at its listen port.

        The same process, so not a stranger. But off-box the listen port is
        unreachable -- the provider publishes it on another -- so LiteLLM
        cannot use it, and it does not stand in for the deployment the server
        needs. Where the two ports are one (local), the deployment is simply
        the right one.
        """
        if port is None or port != self.listen_port or port == self.public_port:
            return False
        return model_name == self.model_name and self.address is not None and normalize_peer_ip(host) == self.address

    @property
    def rental_usd_per_hour(self) -> float | None:
        """What this server's whole machine rents for, dollars per hour; None if unknown."""
        return self.rental and self.rental.usd_per_hour

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
        key_rejected: bool | None = None,
    ) -> None:
        """One probe pass.

        ``ok`` is a 200 from /health: vLLM says it is up. Not "all is well":
        /health takes no key, so a box that refuses ours is ``ok`` and
        Rejected. ``key_rejected`` is /v1/models' verdict on the working key
        this pass, when it was asked; None leaves the last verdict standing.
        """
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
        if self.first_probed_at is None:
            self.first_probed_at = now
        if reached is not None:
            self.reached = reached
        if reached and self.first_reached_at is None:
            self.first_reached_at = now
        if key_rejected is not None:
            self.key_rejected = key_rejected
        if ok and self.key_rejected:
            self.rejected_passes += 1
        else:
            self.rejected_passes = 0
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
            first_probed_at=self.first_probed_at,
            first_reached_at=self.first_reached_at,
            key_rejected=self.key_rejected,
            rejected_passes=self.rejected_passes,
            unlisted=self.unlisted,
        )
        self.state = self_hosted_lifecycle(snap, cfg, now)
        if self.state == HEALTHY and was != HEALTHY:
            _LOGGER.info("fleet server %s@%s healthy", self.name, self.key.display)
        elif self.state in (UNHEALTHY, STALLED) and was not in (UNHEALTHY, STALLED):
            _LOGGER.warning("fleet server %s@%s %s (%s)", self.name, self.key.display, self.state, self.last_error)
        elif self.state == REJECTED and was != REJECTED:
            _LOGGER.warning("fleet server %s@%s up but rejects our key", self.name, self.key.display)
        elif self.state == FOREIGN and was != FOREIGN:
            _LOGGER.warning("fleet server %s@%s foreign: unlisted and rejects our key", self.name, self.key.display)


# What a deployment's cost figure means, when it is not simply money owed.
COST_BASIS_CAPACITY = "derived_capacity"

# The LiteLLM access group every deployment on our own boxes is put in. A
# LiteLLM team can be limited to a group in place of a list of models, and
# the group then follows the fleet: a box added or removed changes what the
# group contains, and nothing that names the group has to be told.
ACCESS_GROUP_SELF_HOSTED = "llmao:self-hosted"


class FleetDeployment:
    """One intended LiteLLM deployment (definitions + api_base).

    Self-hosted: wraps a VllmServer (public port may be unset). Provider: one
    line of config.yaml providers, with the provider's own model string.
    What LiteLLM has and thinks of it (the two LiteLLM sources) is read onto
    it here, not onto VllmServer; the facts are derived from those reads.
    """

    def __init__(
        self,
        *,
        model_name: str,
        name: str,
        self_hosted: bool,
        api_base: str | None = None,
        vllm: VllmServer | None = None,
        provider: str | None = None,
        litellm_model: str | None = None,
    ):
        self.model_name = model_name
        self.name = name
        self.self_hosted = self_hosted
        self._api_base = api_base
        self.vllm = vllm
        # A provider deployment's account (config.yaml providers.<name>) and
        # the provider's model, as LiteLLM names it. None on our boxes.
        self.provider = provider
        self.litellm_model = litellm_model
        # What the last read of the LiteLLM deployments had for this one
        # (read_litellm): the rows stamped with its key or at its dialable
        # address, under its name or another; and, apart, the rows at a
        # box's listen port where that differs from the public one, which
        # LiteLLM cannot reach off-box, so they are noted and not counted.
        self.litellm: tuple[LiteLLMDeployment, ...] = ()
        self.litellm_listen: tuple[LiteLLMDeployment, ...] = ()
        # LiteLLM's own /health verdict on it, from the last read that said
        # anything (Fleet.read_litellm_health). None until one does.
        self.litellm_health: LiteLLMHealth | None = None
        self.skew: list[str] = []
        # The last registration (or removal) call to LiteLLM that failed, and
        # when; cleared by the next one that succeeds.
        self.litellm_error = ""
        self.litellm_error_at: float | None = None

    @classmethod
    def from_vllm(cls, srv: VllmServer) -> FleetDeployment:
        return cls(
            model_name=srv.model_name,
            name=srv.name,
            self_hosted=True,
            vllm=srv,
        )

    @classmethod
    def from_intended(cls, intended: IntendedDeployment) -> FleetDeployment:
        """A provider-line deployment. Our boxes go through from_vllm, with their observed server."""
        return cls(
            model_name=intended.model_name,
            name=intended.name,
            self_hosted=False,
            api_base=intended.api_base,
            provider=intended.provider,
            litellm_model=intended.litellm_model,
        )

    @property
    def key(self) -> DeploymentKey:
        """The deployment's identity: the server's listen URL, or the provider's base, plus our model name."""
        if self.vllm is not None:
            return self.vllm.key
        return deployment_key(self.model_name, api_base=self._api_base)

    @property
    def api_base(self) -> str | None:
        if self.vllm is not None:
            return self.vllm.api_base
        return self._api_base

    @property
    def cost_basis(self) -> str | None:
        """Whether this deployment's cost is a capacity estimate or real money.

        A fact about where the deployment runs, and nothing else. The same
        model can be served from our own boxes or bought from a provider:
        on our boxes the dollars are derived from throughput and are a share
        of capacity already paid for, and from a provider they are a bill.
        None means real money.
        """
        return COST_BASIS_CAPACITY if self.self_hosted else None

    @property
    def access_groups(self) -> set[str]:
        """The LiteLLM access groups this deployment belongs in.

        Like cost_basis, decided by where it runs. A deployment bought from
        a provider is in none, which is what keeps a key limited to the
        self-hosted group from spending money.
        """
        return {ACCESS_GROUP_SELF_HOSTED} if self.self_hosted else set()

    def read_litellm(self, rows: tuple[LiteLLMDeployment, ...]) -> None:
        """Take from one read of the LiteLLM deployments the ones that concern this deployment.

        The stamp decides first: a row stamped with this key is this
        deployment at whatever address it was registered. Then the address:
        an unstamped row at the dialable base is it on our box under any
        name (one box serves one model, so a row at its base is its
        deployment, misnamed when the name differs: config_mismatch), and
        on a provider's base under this model name only, since the account
        serves every model from one base. A row at a box's listen port is
        kept apart (at_listen_port). A box with no address yet is found by
        its stamp alone.
        """
        base = self.api_base and norm_base(self.api_base)
        srv = self.vllm
        mine, listen = [], []
        for row in rows:
            if row.key == self.key:
                mine.append(row)
            elif row.key is not None:
                continue
            elif srv is not None and srv.on_listen_port(row.host, row.port, row.model_name):
                listen.append(row)
            elif base and row.api_base == base and (self.provider is None or row.model_name == self.model_name):
                mine.append(row)
        self.litellm = tuple(mine)
        self.litellm_listen = tuple(listen)

    @property
    def in_litellm(self) -> bool:
        """Whether LiteLLM has this deployment, as of the last read."""
        return bool(self.litellm)

    @property
    def config_mismatch(self) -> bool:
        """LiteLLM has this box's api_base under another model name."""
        return any(row.model_name != self.model_name for row in self.litellm)

    @property
    def at_listen_port(self) -> bool:
        """LiteLLM has this server at its listen port, which is unreachable off-box."""
        return bool(self.litellm_listen)

    @property
    def litellm_healthy(self) -> bool | None:
        """LiteLLM's last /health verdict on it; None when it has said nothing."""
        return self.litellm_health and self.litellm_health.healthy

    @property
    def litellm_health_at(self) -> float | None:
        return self.litellm_health and self.litellm_health.at

    @property
    def litellm_health_display(self) -> str:
        """Human-readable 'litellm_health_at'; a dash when unknown."""
        if self.litellm_health is None:
            return "—"
        return _ago(self.litellm_health.at)

    @property
    def litellm_error_display(self) -> str:
        """Human-readable 'litellm_error_at'; a dash when unknown."""
        if self.litellm_error_at is None:
            return "—"
        return _ago(self.litellm_error_at)


@dataclass(frozen=True)
class Orphan:
    """A LiteLLM deployment whose key no intent has: skew under the key it carries.

    Something instructed LiteLLM to serve it, so it is shown, never deleted
    unattended. Its stamp says who: llmao's own add whose intent has gone
    since (an approval lost in a restart, a ``fleet.hosts`` row removed), or,
    unstamped, a hand add in LiteLLM's UI. Stop tracking on /models deletes
    it. ``passes`` is how many reads running it has been one; the strip
    shows it from ORPHAN_PASSES on, so a transient read is not an item.
    Fleet.read_litellm_deployments builds these.
    """

    deployment: LiteLLMDeployment
    passes: int


@dataclass(frozen=True)
class Discovered:
    """A rental nobody intends: a listed machine with no fleet.hosts row, no approval, no server.

    The account's inventory is not the fleet (another department may rent
    on the same account), so it is never added on sight. It is an attention
    item with what a probe found on it, and Approve. Recomputed every pass
    from the listings; gone when no listing names it.
    """

    rental: Rental
    # One line: the provider's status when it is not running, else what the
    # vLLM listen ports answered ("8001: serving qwen3-8b with our key").
    finding: str
    at: float

    @property
    def name(self) -> str:
        """What the box sends as its host, and so what an approval is keyed by."""
        return self.rental.name


@dataclass(frozen=True)
class Unintended:
    """A machine nobody intends, by the name its box sends, joined from two sources.

    ``rental`` is what its provider reports about it (None when no provider
    reports a machine of that name), with ``finding`` what a probe found on
    it; ``fetch`` is its config fetches with the fleet key (None when it has
    not asked). A machine seen from both sides is one record, with one
    Approve. Fleet.unintended builds these; nothing stores them.
    """

    name: str
    rental: Rental | None
    finding: str
    fetch: ConfigFetch | None


def fetch_detail(fetch: ConfigFetch) -> str:
    """The strip's line for a stranger's config fetches: 'seen 5s · 3 requests'."""
    return f"seen {_ago(fetch.last_at)} · {fetch.count} requests"


class Fleet:
    """Live fleet: vLLM servers, intended deployments, lifecycle probes."""

    def __init__(
        self,
        cfg: Any,
        servers: list[VllmServer],
        deployments: list[FleetDeployment] | None = None,
        models: dict[str, Any] | None = None,
        after_probe=None,
        provisioning: ProvisioningRecord | None = None,
    ):
        self.cfg = cfg
        self.servers = servers
        self.deployments = deployments if deployments is not None else [FleetDeployment.from_vllm(s) for s in servers]
        # model_name → models.yaml row. POST /model/new copies litellm_params
        # from here; LiteLLM /model/info encrypts them on the way back.
        self.models = models or {}
        # Awaitable after each vLLM probe tick (DeploymentManager.sync_selfhost).
        # Wired from create_app so this module does not import the LiteLLM client.
        self.after_probe = after_probe
        # Set when the first lifecycle pass is over: public ports looked up,
        # every server probed, registrations synced. Whatever compares the
        # fleet with LiteLLM waits for it; before then no Vast server has an
        # address, and a comparison reads every one of them as missing.
        self.ready = asyncio.Event()
        # Every name that fetched its config (llmao/provisioning.py). The
        # config route writes it; main.py builds the one record and hands it
        # to both. Each probe pass reads it (read_config_fetches), and
        # unintended() shows the names nothing intends.
        self.provisioning = provisioning or ProvisioningRecord()
        # What an admin told an unknown box to load (intent.Approval), keyed by
        # normalized host. Process memory only; config.yaml is not written.
        self.approvals: dict[str, Approval] = {}
        # The last read of the LiteLLM deployments (llmao/proxy/deployments.py),
        # None until one; and the deployments in it no intent claims, with
        # their pass counts. Both set by read_litellm_deployments.
        self.litellm: LiteLLMDeployments | None = None
        self.orphans: list[Orphan] = []
        # What each provider we rent from last reported (llmao/rental.py).
        # None until read: Vast's comes with every lifecycle pass, RunPod's
        # from its own runner (run_runpod_listing).
        self.vast_rentals: ProviderRentals | None = None
        self.runpod_rentals: ProviderRentals | None = None
        # Rentals nobody intends, by the name the box sends (discover_rentals, every pass).
        self.discovered: dict[str, Discovered] = {}

    @classmethod
    def from_cfg(cls, cfg: Any, models: list, *, provisioning: ProvisioningRecord | None = None) -> Fleet:
        """The fleet config intends: one server and one deployment per intended vLLM process,
        one deployment per provider line. ``models`` is the list loaded at startup; the fleet
        keeps it, and nothing reads models.yaml again. Requires validate_fleet() already ran on cfg."""
        by_name = {model.model_name: model for model in models}
        local = not gpu_host_api_key(cfg, "vast")
        servers: list[VllmServer] = []
        deployments = []
        for intended in intended_deployments(cfg, models):
            if not intended.self_hosted:
                deployments.append(FleetDeployment.from_intended(intended))
                continue
            srv = VllmServer.from_intended(intended)
            if intended.public_port is not None:
                # Deprecated: the listing supplies the public port now.
                # The value seeds it until the box is placed; it is not
                # refused, so a stale row never stops llmao.
                index = sum(1 for s in servers if s.host == srv.host)
                _LOGGER.warning(
                    "fleet.hosts.%s[%s]: the fourth element (public port %s) is deprecated; "
                    "the provider listing supplies it. Drop it from the row.",
                    srv.host,
                    index,
                    intended.public_port,
                )
                srv.public_port = intended.public_port
            elif local:
                srv.public_port = srv.listen_port
            servers.append(srv)
            deployments.append(FleetDeployment.from_vllm(srv))
        return cls(cfg, servers, deployments, models=by_name, provisioning=provisioning)

    def read_config_fetches(self) -> None:
        """Each pass reads the ConfigFetch source: the last fetch by a server's name is its loading start.

        ``config_served_at`` on the server is the lifecycle fact the fetch
        sets, moving it from awaiting contact into the Loading window; the
        probe that follows re-derives the state from it. Copied, not
        referenced: the record may drop the fetch under its cap, the start
        once seen stands.
        """
        for srv in self.servers:
            at = self.provisioning.fetched_at(srv.host)
            if at is not None:
                srv.config_served_at = at

    def read_litellm_deployments(self, snapshot: LiteLLMDeployments) -> None:
        """One read of the LiteLLM deployments, joined to the intended ones; the rest are orphans.

        Each intended deployment takes the rows that are it (FleetDeployment
        .read_litellm). A row nothing took is an orphan, counted across
        reads: one absent from a read drops out, so a flapping one never
        reaches ORPHAN_PASSES, and one that reaches it is logged once, with
        who added it.
        """
        self.litellm = snapshot
        claimed: set[LiteLLMDeployment] = set()
        for dep in self.deployments:
            dep.read_litellm(snapshot.rows)
            claimed.update(dep.litellm)
            claimed.update(dep.litellm_listen)
        before = {(o.deployment.api_base, o.deployment.model_name): o.passes for o in self.orphans}
        orphans = []
        for row in snapshot.rows:
            if row in claimed:
                continue
            seen = before.get((row.api_base, row.model_name), 0) + 1
            orphans.append(Orphan(row, seen))
            if seen == ORPHAN_PASSES:
                _LOGGER.warning(
                    "skew: in LiteLLM, not in config %s %s (%s)",
                    row.display,
                    row.model_name,
                    "llmao added it" if row.key else "not added by llmao",
                )
        self.orphans = orphans

    def read_litellm_health(self, report: LiteLLMHealthReport) -> None:
        """One read of LiteLLM's own /health, onto each deployment it spoke of.

        A deployment with no address this pass has nothing to match the
        answer to, and keeps its last verdict rather than being wiped for the
        four hours until the next check.
        """
        for dep in self.deployments:
            if not dep.api_base:
                continue
            base = norm_base(dep.api_base)
            dep.litellm_health = report.verdict(base, dep.provider and dep.litellm_model)

    def refresh_skew(self, now: float | None = None) -> None:
        """Replace each deployment's stored badges with the ones model_status computes now; a new one is logged."""
        stamp = now or time.time()
        cfg = self.cfg.fleet
        for dep in self.deployments:
            view = deployment_status(snapshot_for_status(dep), cfg, stamp)
            fresh = [badge for badge in view.skew if badge not in dep.skew]
            dep.skew = list(view.skew)
            for badge in fresh:
                _LOGGER.warning("skew: %s@%s %s", dep.name, dep.key.display, SKEW_PHRASE[badge])

    def _materialize_approved(self, host: str) -> None:
        """Servers and deployments for an approved IP that config.yaml does not list.

        One server per approved model, on VLLM_LISTEN_PORTS in order. A port this
        host already has is left alone, so a later approval can add the next one.
        """
        host = normalize_peer_ip(host)
        approval = self.approvals.get(host)
        if not approval:
            return
        members = {normalize_peer_ip(str(ip)) for ip in self.cfg.fleet.hosts}
        if host in members:
            return
        have = {s.listen_port for s in self.servers if s.host == host}
        for intended in host_intents(self.cfg, self.models, host, approved=list(approval.models)):
            if intended.listen_port not in have:
                self._add_server(intended)

    def _add_server(self, intended: IntendedDeployment) -> VllmServer:
        srv = VllmServer.from_intended(intended)
        self.servers.append(srv)
        self.deployments.append(FleetDeployment.from_vllm(srv))
        return srv

    def approval_display(self, host: str) -> str:
        """'Approved · 15h ago' beside the rows of an approved host; "" for a fleet.hosts member."""
        approval = self.approvals.get(normalize_peer_ip(host))
        return (approval and f"Approved · {_ago(approval.at)} ago") or ""

    def config_fetch_display(self, host: str) -> str:
        """Human-readable age of the last config fetch for a host, "—" if never."""
        at = self.provisioning.fetched_at(host)
        if at is None:
            return "—"
        return _ago(at)

    def approve_models(self, host: str, names: list[str], *, now: float | None = None) -> Approval:
        """An admin names the models an unknown box loads: the host is intended from here on.

        It leaves the strip and gets its servers at once (Awaiting, one per
        model on VLLM_LISTEN_PORTS in order), so it reads with the other
        deployments; its next config fetch is served. Replaces any
        previous approval. Raises, changing nothing, when the host is already
        in fleet.hosts, the selection is empty, or a name is unknown or not
        self-hosted. Lost on restart.
        """
        host = normalize_peer_ip(host)
        members = {normalize_peer_ip(str(ip)) for ip in self.cfg.fleet.hosts}
        if host in members:
            raise ValueError(f"{host} is already in fleet.hosts")
        chosen = [str(n).strip() for n in names if str(n).strip()]
        if not chosen:
            raise ValueError("Select at least one model")
        if len(chosen) > len(VLLM_LISTEN_PORTS):
            raise ValueError(f"{host}: at most {len(VLLM_LISTEN_PORTS)} servers")
        for name in chosen:
            model = self.models.get(name)
            if model is None:
                raise ValueError(f"Unknown model: {name}")
            if VllmRecipe.from_model(model) is None:
                raise ValueError(f"{name} has no vLLM recipe, so a box cannot load it")
        approval = self.approvals[host] = Approval(host, tuple(chosen), now or time.time())
        self._materialize_approved(host)
        return approval

    def intended_names(self) -> set[str]:
        """Every name intent covers: the fleet.hosts keys, the approvals, and the hosts of the servers."""
        names = {normalize_peer_ip(str(key)) for key in self.cfg.fleet.hosts} | set(self.approvals)
        return names | {s.host for s in self.servers}

    def unintended(self) -> list[Unintended]:
        """Every machine nobody intends, one record per name: discovered rentals and stranger fetches, joined.

        A stranger is a ConfigFetch whose name nothing intends; the record
        keeps every fetch and the split is made here. A name both sources
        know is one record. The ones that asked for their config come
        first, latest ask first; then the rest by name.
        """
        names = self.intended_names()
        strangers = {name: fetch for name, fetch in self.provisioning.fetches.items() if name not in names}
        out = []
        for name in set(self.discovered) | set(strangers):
            found = self.discovered.get(name)
            out.append(Unintended(name, found and found.rental, (found and found.finding) or "", strangers.get(name)))
        return sorted(out, key=lambda u: (u.fetch is None, -((u.fetch and u.fetch.last_at) or 0.0), u.name))

    def model_rollup(self, model_name: str, *, now: float | None = None):
        """Available / Degraded / Unavailable. validate_fleet already required the intervals."""
        stamp = now or time.time()
        cfg = self.cfg.fleet
        views = [deployment_status(snapshot_for_status(dep), cfg, stamp) for dep in self.deployments]
        return model_status(model_name, views)

    def cost_basis(self, model_name: str) -> str | None:
        """How to read a cost recorded against this model name.

        Usage is recorded per model name, not per deployment, so this is as
        fine as it can be told apart. A capacity estimate only when every
        deployment of the name is one: where a name is served both ways,
        some of its dollars are real, and calling the lot an estimate would
        hide a bill. A name with no deployment is likewise treated as money.
        """
        bases = {d.cost_basis for d in self.deployments if d.model_name == model_name}
        return COST_BASIS_CAPACITY if bases == {COST_BASIS_CAPACITY} else None

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
        stamp = now or time.time()
        own = client is None
        if own:
            client = httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        self.read_config_fetches()
        try:
            in_litellm_by_srv = {dep.vllm: dep.in_litellm for dep in self.deployments if dep.vllm is not None}
            for srv in self.servers:
                url = srv.health_url
                if url is None:
                    # No public port: nothing to probe. Leave the state alone.
                    # An unlisted host has no port because it has no instance,
                    # which place_servers has already said once. Nothing to
                    # dial is the strongest form of not reached, so it still
                    # counts as an attempt: the grace clock runs, and the host
                    # reads Stalled once it is out, as a dead rental should.
                    if srv.unlisted:
                        srv.record_probe(
                            False,
                            now=stamp,
                            grace_s=grace,
                            fail_threshold=threshold,
                            err="no address to dial",
                            reached=False,
                            in_litellm=bool(in_litellm_by_srv.get(srv, False)),
                        )
                        continue
                    _LOGGER.warning(
                        "fleet: no health url for %s@%s listen %s",
                        srv.name,
                        srv.host,
                        srv.listen_port,
                    )
                    continue
                was = srv.state
                ok, reached, err = await _get_health(client, url)
                # /v1/models with the working key, when /health passed: the
                # served window on the edge into Healthy, and again if an
                # earlier read came back empty (it is fixed at engine init,
                # so every probe would be waste); and on every pass while the
                # key was refused, so Rejected ends the pass the key is
                # fixed, or while no listing names the host, so a reassigned
                # address is caught. The KV cache size comes from the sampler.
                key_rejected = None
                ask = was != HEALTHY or srv.observed_max_model_len is None or srv.key_rejected or srv.unlisted
                if ok and ask:
                    served = await fetch_served_models(client, srv.root_url, srv.api_key)
                    if served.status == 401:
                        key_rejected = True
                    elif served.status == 200:
                        key_rejected = False
                        if served.max_model_len is not None:
                            srv.observed_max_model_len = served.max_model_len
                            srv.observed_at = stamp
                srv.record_probe(
                    ok,
                    now=stamp,
                    grace_s=grace,
                    fail_threshold=threshold,
                    err=err,
                    reached=reached,
                    in_litellm=bool(in_litellm_by_srv.get(srv, False)),
                    key_rejected=key_rejected,
                )
        finally:
            if own:
                await client.aclose()

    async def read_vast_rentals(self, client: httpx.AsyncClient) -> None:
        """Read what Vast reports about our instances into vast_rentals. Nothing when Vast is not configured."""
        key = gpu_host_api_key(self.cfg, "vast")
        if not key:
            return
        self.vast_rentals = ProviderRentals("Vast", rentals_from_body(await fetch_instances(key, client=client)))

    async def read_runpod_rentals(self, client: httpx.AsyncClient) -> None:
        """Read what RunPod reports about our pods into runpod_rentals. Raises if RunPod does not answer."""
        key = gpu_host_api_key(self.cfg, "runpod")
        self.runpod_rentals = ProviderRentals("RunPod", rentals_from_pods(await fetch_pods(key, client=client)))

    async def run_runpod_rentals(self) -> None:
        """Runner: what RunPod reports about our pods, every RUNPOD_LISTING_INTERVAL_S.

        Kept apart from the lifecycle so that RunPod is asked on its own
        schedule rather than on every probe pass; the lifecycle applies the
        latest list it has. Pods do not move: a new one waits at most an
        interval to be placed, and it is still provisioning by then. Ends at
        once when no RunPod key is configured.
        """
        if not gpu_host_api_key(self.cfg, "runpod"):
            return
        while True:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                    await self.read_runpod_listing(client)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # The last list stands; an outage only makes it older.
                _LOGGER.warning("runpod listing failed: %s", e)
            await asyncio.sleep(RUNPOD_LISTING_INTERVAL_S)

    def place_servers(self) -> None:
        """Put each server on its Rental from the latest provider sets, and decide which are unlisted.

        Unlisted means every provider we rent from has answered with its
        list and none has this host for UNLISTED_PASSES passes running. With
        a list missing (an outage, or RunPod not read yet) nothing is
        concluded, and the last verdict stands. Logged when the verdict
        changes, not on every pass.

        A single absence is not a conclusion, learned from one incident: a
        RunPod pod is missing from the pod list while it provisions, over 90
        minutes in the case measured. Before unlisted drove deletion that was
        cosmetic. Then it deleted the route of a box that was up and serving,
        which the next pass re-added -- the add fires on healthy-and-absent,
        the delete on unlisted -- so the two alternated for as long as the
        listing gap lasted. Observed on qwen3.8-27b at 47 seconds a cycle, 33
        times in an hour, unroutable through the gateway for half of each
        cycle while /fleet showed it serving and a direct completion against
        the box passed.

        A host no list has ever named is unlisted like any other once the
        passes run. A provider with no key configured is not one we rent
        from, so a host rented there would be called unlisted for ever; the
        cost is a sentence on the attention strip, never a deletion, since
        the delete fires on unlisted-and-not-Healthy and a box that is
        Unhealthy, Stalled, Rejected or Foreign is deleted by the probe gate
        anyway. Guarding against it cost a restart hole: a rental that ended
        while llmao was down was, after the restart, a host no listing had
        ever named, and the sentence never showed.
        """
        vast_wanted = bool(gpu_host_api_key(self.cfg, "vast"))
        runpod_wanted = bool(gpu_host_api_key(self.cfg, "runpod"))
        placed = self.apply_rentals(self.runpod_rentals, self.vast_rentals)
        # Neither provider configured (a local fleet): nothing is unlisted.
        complete = (vast_wanted or runpod_wanted) and (
            (not vast_wanted or self.vast_rentals is not None)
            and (not runpod_wanted or self.runpod_rentals is not None)
        )
        if not complete:
            return
        for srv in self.servers:
            listed = srv in placed
            srv.unlisted_passes = 0 if listed else srv.unlisted_passes + 1
            unlisted = srv.unlisted_passes >= UNLISTED_PASSES
            if unlisted != srv.unlisted:
                if unlisted:
                    _LOGGER.warning("fleet: no provider lists %s; the rental may have ended", srv.host)
                else:
                    _LOGGER.info("fleet: %s is listed again", srv.host)
            srv.unlisted = unlisted
            if unlisted:
                # Its old machine, and that machine's price, no longer apply.
                srv.rental = None

    def apply_rentals(self, *sets: ProviderRentals | None) -> list[VllmServer]:
        """Put each server on its Rental from the providers' sets; the servers some provider names.

        The matching rule is rental.match_rental: by address, else the same
        machine as last time. A server that matches is placed even when its
        Rental is stopped or initializing and names no address; only a
        server no provider names at all is unplaced this pass.
        """
        placed = []
        for srv in self.servers:
            rental = match_rental(srv.host, srv.rental, *sets)
            if rental is None:
                continue
            placed.append(srv)
            # The provider's report is where the address and the public port
            # come from. A Rental with no address or no map (a stopped pod)
            # says nothing; the last values stand until the report has them
            # again.
            if rental.host:
                srv.address = rental.host
            if rental.ports:
                _set_public_port(srv, rental.ports, rental.provider.lower())
            if srv.rental != rental:
                _LOGGER.info(
                    "rental: %s is %s %s (%s), %s at %s, $%s/hr",
                    srv.host,
                    rental.provider,
                    rental.id,
                    rental.label,
                    rental.status or "status unknown",
                    rental.host or "no address",
                    rental.usd_per_hour,
                )
                srv.rental = rental
        return placed

    async def discover_rentals(self, client: httpx.AsyncClient, *, now: float | None = None) -> None:
        """Rentals nobody intends, with what a probe finds on each: Fleet.discovered, rebuilt every pass.

        A listed machine that no server sits on and whose name (what its box
        sends as its host) and address are neither in fleet.hosts nor
        approved. Adoption requires an admin's approval, so nothing here
        adds a server: the /models strip shows the finding with Approve,
        keyed by the name. A rental with no name yet (a Vast instance
        without an address) has nothing to approve and is not listed.
        """
        stamp = now or time.time()
        names = self.intended_names()
        on = {(s.rental.provider, s.rental.id) for s in self.servers if s.rental is not None}
        found: dict[str, Discovered] = {}
        for rentals in (self.vast_rentals, self.runpod_rentals):
            for rental in rentals or ():
                # The name is the provider client's word, not a peer address:
                # it is compared as given. Only an address is normalized.
                name = rental.name
                address = normalize_peer_ip(rental.host)
                if not name or name in names or (address and address in names) or (rental.provider, rental.id) in on:
                    continue
                found[name] = Discovered(rental, await self._discovery_finding(client, rental, address), stamp)
        if set(found) != set(self.discovered):
            _LOGGER.info("fleet: rentals nobody intends: %s", sorted(found) or "none")
        self.discovered = found

    async def _discovery_finding(self, client: httpx.AsyncClient, rental: Rental, host: str) -> str:
        """One line on a rental nobody intends: its status when not running, else what its vLLM ports answered."""
        if rental.status and not rental.running:
            return rental.status
        if not host:
            return "no address yet"
        ports = sorted(
            (int(listen), int(public)) for listen, public in rental.ports.items() if int(listen) in VLLM_LISTEN_PORTS
        )
        if not ports:
            return "no vLLM port published"
        parts = []
        reached_any = False
        for listen, public in ports:
            ok, reached, err = await _get_health(client, f"http://{host}:{public}/health")
            reached_any = reached_any or reached
            if not reached:
                parts.append(f"{listen}: {err}")
            elif not ok:
                parts.append(f"{listen}: vLLM answering, not serving ({err})")
            else:
                parts.append(f"{listen}: {await self._served_with_our_key(client, rental, host, listen, public)}")
        if not reached_any:
            return "nothing listening"
        return "; ".join(parts)

    async def _served_with_our_key(
        self, client: httpx.AsyncClient, rental: Rental, host: str, listen: int, public: int
    ) -> str:
        # The bearer is minted from the name the box sends, not from where it is dialed.
        served = await fetch_served_models(
            client, f"http://{host}:{public}", vllm_api_key(self.cfg, rental.name, listen)
        )
        if served.status == 401:
            return "refuses our key"
        if served.status != 200:
            return f"up, /v1/models answered {served.status or 'nothing usable'}"
        return f"serving {self._model_name_for(served.hf_id) or served.hf_id or 'an unnamed model'} with our key"

    def _model_name_for(self, hf_id: str) -> str:
        """The models.yaml name whose recipe runs these weights, or ""."""
        for model in self.models.values():
            recipe = VllmRecipe.from_model(model)
            if recipe is not None and recipe.hf_model == hf_id:
                return str(model.model_name)
        return ""

    async def run_lifecycle(self) -> None:
        interval = float(self.cfg.fleet.health_interval_s)
        timeout = float(self.cfg.fleet.health_timeout_s)
        while True:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
                    try:
                        await self.read_vast_rentals(client)
                    except Exception:
                        _LOGGER.exception("the Vast rentals could not be read")
                    self.place_servers()
                    await self.discover_rentals(client)
                    await self.probe_all(client=client)
                    if self.after_probe is not None:
                        await self.after_probe()
            except Exception:
                _LOGGER.exception("fleet lifecycle probe failed")
            # Even after a failed pass: waiting on a fleet that never answers
            # would stop the comparisons for good, and they report the failure.
            self.ready.set()
            await asyncio.sleep(interval)


def snapshot_for_status(dep) -> DeploymentSnapshot:
    """The last stored observations for this deployment, as the one record model_status reads.

    record_probe keeps the counters (last_probe_ok, fails, last_ok,
    vllm_probed_at) and config_served_at on the server, and the two LiteLLM
    reads sit on the deployment, so deployment_status can re-run the same
    lifecycle function the probe loop uses -- at the current time, so a
    Loading deployment that has since outlasted the grace window reads
    Stalled without waiting for the next probe.
    """
    srv = dep.vllm
    if srv is None:
        return DeploymentSnapshot(
            model_name=dep.model_name,
            self_hosted=False,
            in_litellm=dep.in_litellm,
            litellm_healthy=dep.litellm_healthy,
            litellm_health_at=dep.litellm_health_at,
            config_mismatch=dep.config_mismatch,
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
        litellm_health_at=dep.litellm_health_at,
        context_window=srv.observed_max_model_len or srv.max_model_len,
        kv_cache_tokens=srv.kv_cache_tokens,
        config_mismatch=dep.config_mismatch,
        at_listen_port=dep.at_listen_port,
        config_served_at=srv.config_served_at,
        reached=srv.reached,
        first_probed_at=srv.first_probed_at,
        first_reached_at=srv.first_reached_at,
        key_rejected=srv.key_rejected,
        rejected_passes=srv.rejected_passes,
        unlisted=srv.unlisted,
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
    if dep.vllm.state in (REJECTED, FOREIGN):
        return "vLLM rejects our key"
    if dep.vllm.state != HEALTHY:
        return "vLLM is not serving yet"
    if not dep.api_base:
        return "its public address is not known yet"
    return None


def remove_refusal(dep) -> str | None:
    """Why the Fleet Remove button must not call delete_deployment.

    None means the row is self-hosted, is already in LiteLLM, and vLLM is
    Unhealthy, Stalled, Rejected or Foreign — the same gate the automatic
    sync_selfhost applies. delete_deployment itself does not apply this gate.

    "The same gate" is load-bearing, and was briefly untrue: e1567cb let
    sync_selfhost delete an unlisted server's route while it was still
    serving, which this function would have refused. A serving box keeps its
    route in both paths.
    """
    if not dep.self_hosted or dep.vllm is None:
        return "not a self-hosted deployment"
    if not dep.in_litellm:
        return "not in LiteLLM"
    if dep.vllm.state not in (UNHEALTHY, STALLED, REJECTED, FOREIGN):
        return "vLLM is still healthy"
    return None


def orphan_refusal(orphan: Orphan) -> str | None:
    """Why Stop tracking must not delete an orphan: only when LiteLLM reported it without an id."""
    return (not orphan.deployment.id and "LiteLLM reported it without an id") or None


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


async def fetch_served_models(client: httpx.AsyncClient, base: str, api_key: str) -> edict:
    """GET /v1/models with the key we believe this vLLM has: (status, max_model_len, hf_id).

    ``status`` is the HTTP status, or None when nothing usable came back. A
    401 is the box refusing the key (Rejected); a 200 carries the served
    context window, ``max_model_len``, which is None when the body has none,
    and ``hf_id``, the first served model's id ("" when the body names none).

    base is the server root, not api_base: a /v1-suffixed base would ask for
    /v1/v1/models and 404. The KV cache size, the other half of the oversized
    check, is read by the sampler (llmao/sampler.py), the one reader of
    /metrics.

    Never raises: an unreachable or differently-shaped endpoint leaves both
    unknown and the UX shows them as such.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    served = edict(status=None, max_model_len=None, hf_id="")
    try:
        resp = await client.get(f"{base}/v1/models", headers=headers)
        served.status = resp.status_code
        if resp.status_code == 200:
            body = resp.json()
            served.max_model_len = parse_max_model_len(body)
            rows = body.get("data") if isinstance(body, dict) else None
            served.hf_id = next((str(r["id"]) for r in rows or () if isinstance(r, dict) and r.get("id")), "")
    except (httpx.HTTPError, ValueError):
        pass
    return served


async def _get_health(client: httpx.AsyncClient, url: str) -> tuple[bool, bool, str | None]:
    """(ok, reached, err). ``reached`` is True when vLLM's HTTP server answered
    at all (e.g. a 503 while the model is still loading); False for no
    connection. ``ok`` is only a 200.
    """
    try:
        resp = await client.get(url)
    except httpx.TimeoutException as e:
        _LOGGER.warning("health %s: %s", url, e)
        return False, False, "timeout"
    except httpx.HTTPError as e:
        _LOGGER.warning("health %s: %s", url, e)
        return False, False, "no connection"
    if resp.status_code == 200:
        return True, True, None
    return False, True, f"HTTP {resp.status_code}"

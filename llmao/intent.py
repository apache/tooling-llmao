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

"""What we intend to serve, and from where: the ``IntendedDeployment`` source.

One ``IntendedDeployment`` per intended LiteLLM deployment, from three
origins, all of them config:

- a ``fleet.hosts`` row in config.yaml: one vLLM process on one of our
  boxes, ``[model, port]`` or ``[model, port, name]``;
- an approval, held in memory by the fleet: an unknown box an admin told
  to load some models, one process per model on the listen ports in
  order;
- a ``config.yaml`` provider line: a model bought from a provider account.

``models.yaml`` supplies the recipe for our boxes (``VllmRecipe``). This
module is the only one that reads a host row or a provider line; every
other module reads the record. The record carries everything derivable
from config at parse time, and that includes the bearer: it is minted
here, once, and the payload, the probe and the executor read
``api_key`` and derive nothing. Nothing intended is ever called a server.

``providers`` in config.yaml holds our accounts, of two kinds. A model
provider serves models through its API (xAI, OpenRouter, Anthropic,
Bedrock: any account with an ``api_base``) and lists the ones it serves
for us, each line one deployment::

    providers:
      openrouter:
        api_key: sk-or-...
        api_base: https://openrouter.ai/api/v1
        models:
          grok-4: openrouter/x-ai/grok-4      # model_name: the provider's model, as LiteLLM names it

A GPU host rents us the boxes our own vLLM servers run on (Vast, RunPod).
It is known by name (llmao has an API client for each, in GPU_HOSTS) and
carries only its api_key; llmao reads its API for port mappings and
prices::

    providers:
      vast:
        api_key: ...

Where a model is served is a fact about the deployment, never the model, so
none of this is in models.yaml. The same model can be listed under several
providers and run on our own boxes as well; which one serves a request is
LiteLLM's routing, steered by ``order`` (fleet.ORDER_SELF_HOSTED and
fleet.ORDER_SPEND). The keys live in config.yaml, which is not committed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from llmao.models import VllmRecipe, load_models
from llmao.reconcile import DeploymentKey, deployment_key, normalize_peer_ip

_LOGGER = logging.getLogger(__name__)

# The GPU hosts llmao has an API client for. An account under one of these
# names rents boxes; any other name is a model provider.
GPU_HOSTS = ("vast", "runpod")

# The three container ports a box's template exposes for vLLM, in order (the
# Vast template and the RunPod image alike). A box does not get a fourth. A
# host row may name any of them; an approval takes them in order. Either
# provider's listing maps each one to the public port a server is dialed on.
VLLM_LISTEN_PORTS = (8001, 8002, 8003)

# Boxes already up on other ports. Delete a pair when that box is replaced.
# Not a template for new hosts. These run the shared fleet.selfhost_api_key,
# which validate_fleet requires while one of them is configured; every other
# box runs the key derived from the salt. vllm_api_key applies the rule.
LEGACY_SERVERS = {
    ("80.188.223.202", 10100),  # gemma4-26b, public port from Vast
    ("103.196.86.105", 8004),  # qwen3.8-27b, public port from RunPod
    # 8003 is a normal listen port. This box still runs the shared key.
    ("213.173.110.78", 8003),
}

# Where an IntendedDeployment came from.
HOSTS = "hosts"  # a fleet.hosts row
APPROVAL = "approval"  # an admin approved an unknown box for these models
PROVIDER = "provider"  # a config.yaml provider line
# A Vast address found serving a model nobody intended, taken in as if it
# had been (Fleet.adopt_vast_instances). Whether adoption survives is the
# Deployment work's call; until then it is one shape with the rest.
ADOPTED = "adopted"


class UnknownHostError(KeyError):
    """No fleet.hosts entry and no approval for this host."""


@dataclass(frozen=True)
class Approval:
    """An admin vouched for a host not in fleet.hosts and named the models it loads.

    The one intent that changes at runtime. Process memory on ``Fleet``,
    lost on restart; ``fleet.hosts`` is the durable gate. ``models`` take
    ``VLLM_LISTEN_PORTS`` in order.
    """

    host: str
    models: tuple[str, ...]
    at: float


@dataclass(frozen=True)
class IntendedDeployment:
    """One intended LiteLLM deployment, as config says it.

    On our boxes (``recipe`` set): ``host`` and ``listen_port`` are the
    identity, ``name`` labels the process, ``api_key`` is the bearer it
    runs with. From a provider (``provider`` set): ``api_base`` is the
    identity, ``litellm_model`` the provider's model as LiteLLM names it,
    and ``name`` is ``model@provider``, unique where the model name is not
    (grok-4 can come from xAI and from OpenRouter).
    """

    key: DeploymentKey
    model_name: str
    name: str
    origin: str
    host: str | None = None
    listen_port: int | None = None
    recipe: VllmRecipe | None = None
    api_key: str = ""
    provider: str | None = None
    litellm_model: str | None = None
    api_base: str | None = None
    # Deprecated: a fourth host-row element pinning the public port. It
    # seeds the port until a listing names the box; see parse_host_row.
    public_port: int | None = None

    @property
    def self_hosted(self) -> bool:
        return self.recipe is not None


# --- the bearer ------------------------------------------------------------
#
# Derived, not stored. The salt stays in llmao config; fleet.key is only the
# bearer for GET /vllm/config, so a box that copied FLEET_KEY cannot compute
# another box's key. The payload carries the derived bearer; that is how
# Vast boxes receive it (instance env injection does not work). The box
# needs it before `vllm serve` and LiteLLM needs the same value at
# /model/new, so it is computable on both sides from the host and listen
# port alone; llmao keeps no key table. Prefix sk-vllm- marks the key as a
# vLLM server bearer.
#
# HMAC, not HKDF (one output). Not SHA256(secret||msg). Not a Postgres table.
# Changing the salt invalidates every derived bearer.

# Domain label: other secrets from fleet.vllm_api_salt must use a different string.
_LABEL = "llmao.vllm.api_key"


def derive_vllm_api_key(salt: str, host: str, listen_port: int) -> str:
    """sk-vllm- + urlsafe-b64(HMAC-SHA256(salt, label\\nhost\\nport)), no pad.

    The salt was checked by validate_fleet at startup (present, not the
    placeholder); this trusts it. Host and port are checked because the
    CLI hands them in by hand.
    """
    key = salt.strip()
    ip = normalize_peer_ip(host)
    if not ip:
        raise ValueError("host is empty")
    if not isinstance(listen_port, int) or listen_port <= 0:
        raise ValueError(f"listen_port must be a positive int, got {listen_port!r}")
    msg = f"{_LABEL}\n{ip}\n{listen_port}".encode()
    digest = hmac.new(key.encode(), msg, hashlib.sha256).digest()
    return "sk-vllm-" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def vllm_api_key(cfg: Any, host: str, listen_port: int) -> str:
    """The bearer this vLLM process runs with, and so what every reader of it uses.

    The shared fleet.selfhost_api_key for LEGACY_SERVERS, the key derived
    from fleet.vllm_api_salt for every other box. One rule, applied once,
    when the IntendedDeployment is minted: the payload tells the box this
    key, the probe asks /v1/models with it, LiteLLM is registered with it.
    The wrong key 401s, and the next probe says so (Rejected).

    Not fleet.key (boxes have that). Not a models.yaml field. The salt is
    required and validate_fleet checked it at startup; read it as an
    attribute, never with a default, so a config that skipped validation
    raises here rather than HMAC an empty salt into a public bearer.
    """
    if (normalize_peer_ip(host), int(listen_port)) in LEGACY_SERVERS:
        return str(cfg.fleet.selfhost_api_key).strip()
    return derive_vllm_api_key(cfg.fleet.vllm_api_salt, host, listen_port)


# --- our boxes: fleet.hosts rows and approvals -----------------------------


def parse_host_row(raw: Any, host: str, index: int) -> tuple[str, int, str, int | None]:
    """[model, port] | [model, port, name] | [model, port, name, public_port]

    The fourth element is deprecated. It pinned the public port for RunPod
    boxes while their mapping could not be looked up; now both providers'
    listings carry it (Vast's HostPort, RunPod's portMappings). A row that
    still has one loads, with a warning from Fleet.from_cfg, and the value
    only seeds public_port until a listing names the box. Drop it from
    config; it is not refused so that a config change never stops llmao.
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


def vllm_intent(
    cfg: Any,
    host: str,
    port: int,
    name: str,
    model: Any,
    *,
    origin: str,
    public_port: int | None = None,
) -> IntendedDeployment:
    """One vLLM process on one of our boxes, minted with its recipe and its bearer."""
    model_name = str(model.model_name)
    return IntendedDeployment(
        key=deployment_key(model_name, host=host, listen_port=port),
        model_name=model_name,
        name=name,
        origin=origin,
        host=host,
        listen_port=int(port),
        recipe=VllmRecipe.from_model(model),
        api_key=vllm_api_key(cfg, host, port),
        public_port=public_port,
    )


def host_intents(cfg: Any, by_name: dict[str, Any], host: str, *, approved: list[str] | None = None) -> list:
    """The IntendedDeployments for one host: its fleet.hosts rows, else its approval.

    A host absent from fleet.hosts is served from ``approved``: one process
    per model, listen ports in order, named after the model. That list is
    the in-memory approval; config.yaml is not consulted and not written.
    Raises UnknownHostError when the host has neither. Requires
    validate_fleet() already ran on cfg.
    """
    host = normalize_peer_ip(host)
    known = bool(host) and host in cfg.fleet.hosts
    if not host or (not known and not approved):
        raise UnknownHostError(host)
    if known:
        out = []
        for i, raw in enumerate(cfg.fleet.hosts[host]):
            model_name, port, name, public = parse_host_row(raw, host, i)
            out.append(vllm_intent(cfg, host, port, name, by_name[model_name], origin=HOSTS, public_port=public))
        return out
    if len(approved) > len(VLLM_LISTEN_PORTS):
        raise ValueError(f"{host}: at most {len(VLLM_LISTEN_PORTS)} servers")
    return [
        vllm_intent(cfg, host, port, name, by_name[name], origin=APPROVAL)
        for name, port in zip(approved, VLLM_LISTEN_PORTS, strict=False)
    ]


# --- providers: config.yaml accounts and lines -----------------------------


def provider_api_key(cfg: Any, name: str) -> str:
    """config.yaml providers.<name>.api_key, or "" if it is not there."""
    entry = (cfg.get("providers") or {}).get(name) or {}
    return str(entry.get("api_key") or "").strip()


def gpu_host_api_key(cfg: Any, name: str) -> str:
    """The API key for a GPU host account, or "" when there is none.

    Read from providers.<name>.api_key, else from fleet.<name>.api_key, where
    the Vast key used to live (validate_providers warns about that).
    """
    key = provider_api_key(cfg, name)
    if key:
        return key
    old = ((cfg.get("fleet") or {}).get(name)) or {}
    return str(old.get("api_key") or "").strip() if isinstance(old, dict) else ""


def provider_intent(provider: str, model_name: str, litellm_model: str, api_base: str) -> IntendedDeployment:
    """One model served through one provider account."""
    return IntendedDeployment(
        key=deployment_key(model_name, api_base=api_base),
        model_name=model_name,
        name=f"{model_name}@{provider}",
        origin=PROVIDER,
        provider=provider,
        litellm_model=litellm_model,
        api_base=api_base,
    )


def provider_intents(cfg: Any) -> list[IntendedDeployment]:
    """Every deployment listed under config.yaml providers. Assumes validate_providers ran."""
    out = []
    for name, entry in (cfg.get("providers") or {}).items():
        if name in GPU_HOSTS:
            continue
        api_base = str(entry.api_base).strip()
        for model_name, litellm_model in entry.models.items():
            out.append(provider_intent(str(name), str(model_name), str(litellm_model).strip(), api_base))
    return out


# --- all of it -------------------------------------------------------------


def intended_deployments(
    cfg: Any,
    models: list | None = None,
    approved_models: dict[str, list[str]] | None = None,
) -> list[IntendedDeployment]:
    """Every IntendedDeployment: fleet.hosts rows, then approvals, then provider lines.

    The one function over all three origins. Requires validate_fleet()
    already ran on cfg.
    """
    models = models or load_models(cfg=cfg)
    by_name = {str(model.model_name): model for model in models}
    out = []
    members = set()
    for host in cfg.fleet.hosts:
        host = normalize_peer_ip(str(host))
        members.add(host)
        out.extend(host_intents(cfg, by_name, host))
    for host, names in (approved_models or {}).items():
        host = normalize_peer_ip(host)
        if host in members or not names:
            continue
        out.extend(host_intents(cfg, by_name, host, approved=names))
    out.extend(provider_intents(cfg))
    return out


def validate_providers(cfg: Any, models: list) -> None:
    """Fail-fast if config.yaml providers is malformed or names an unknown model.

    The isinstance checks are for what a hand-written config.yaml can hold
    where a mapping belongs: a string, a list, or nothing at all.
    """
    providers = cfg.get("providers")
    fleet = cfg.get("fleet") or {}
    for name in GPU_HOSTS:
        if name not in fleet:
            continue
        if isinstance(providers, dict) and name in providers:
            raise ValueError(f"config.yaml: the {name} key is in both fleet.{name} and providers.{name}; keep one")
        old = fleet[name]
        if not isinstance(old, dict) or not str(old.get("api_key") or "").strip():
            raise ValueError(f"config.yaml: fleet.{name}.api_key is required")
        _LOGGER.warning("config.yaml: fleet.%s.api_key is deprecated; move it to providers.%s.api_key", name, name)
    if providers is None:
        return
    if not isinstance(providers, dict):
        raise ValueError("config.yaml: providers must be a mapping of provider name to account")
    known = {str(model.model_name) for model in models}
    for name, entry in providers.items():
        where = f"config.yaml: providers.{name}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be a mapping with api_key, api_base and models")
        key = str(entry.get("api_key") or "").strip()
        if not key or key.startswith("CHANGE_ME"):
            raise ValueError(f"{where}.api_key is required")
        if name in GPU_HOSTS:
            # A GPU host rents boxes; it serves no model by itself.
            extra = sorted(k for k in entry if k != "api_key")
            if extra:
                raise ValueError(f"{where} is a GPU host and takes only api_key, not {', '.join(extra)}")
            continue
        base = urlparse(str(entry.get("api_base") or "").strip())
        if base.scheme not in ("http", "https") or not base.netloc:
            raise ValueError(f"{where}.api_base must be an http(s) URL")
        served = entry.get("models")
        if not isinstance(served, dict):
            raise ValueError(f"{where}.models must be a mapping of model_name to the provider's model")
        for model_name, litellm_model in served.items():
            if str(model_name) not in known:
                raise ValueError(f"{where}.models: {model_name} is not in models.yaml")
            if not str(litellm_model or "").strip():
                raise ValueError(f"{where}.models.{model_name} needs the provider's model, e.g. xai/grok-4")

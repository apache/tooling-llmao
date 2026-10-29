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

"""The server side of a rental provisioning itself.

A rental provisions itself with one request: ``provision.sh`` runs,
``install_set.py`` fetches ``GET /vllm/config`` with the fleet key, and
the rental installs what it gets (docs/box-protocol.md). This module is
the whole server side of that request, and the route in ``api.py`` is a
few lines calling it:

- ``fetch_refusal``: the fleet-key check.
- ``rental_name``: how a rental names itself (``X-LLMAO-Host`` as given,
  else the forwarded address, else the peer).
- ``ProvisioningRecord``: one ``ConfigFetch`` per name that ever fetched,
  intended or not. One of the six reconcile sources
  (docs/fleet-reconcile-design.md, Vocabulary). Whether a name is
  intended is the join's call, not this record's: the fleet reads the
  record each pass, a Deployment takes its loading start from it, and a
  name nothing intends is shown as a stranger.
- ``config_for_host``: the payload, the host's IntendedDeployments
  (llmao/intent.py) rendered as the servers JSON the rental installs. It
  derives nothing; the bearer and the recipe are on the record.
"""

from __future__ import annotations

import hmac
import logging
import time
from dataclasses import dataclass, replace
from typing import Any

from easydict import EasyDict as edict  # noqa: N813

from llmao.intent import IntendedDeployment, host_intents
from llmao.reconcile import normalize_peer_ip

_LOGGER = logging.getLogger(__name__)

# The rental's name, as its provider's provision.sh sets it (a Vast
# instance id, a RunPod pod id). Not X-Forwarded-For.
HOST_HEADER = "X-LLMAO-Host"

# How many names are remembered. A scan of the fleet key's port from
# strangers must not grow without bound; the oldest last-seen go.
FETCH_CAP = 50


def fetch_refusal(cfg: Any, authorization: str | None) -> tuple[int, str] | None:
    """Why a config fetch is refused, as (status, message); None when the bearer is the fleet key.

    ``fleet.key`` is one shared secret, baked into every rental's template,
    checked present by validate_fleet at startup. Missing from the request,
    or wrong by a constant-time compare: 403.
    """
    auth = authorization or ""
    prefix = "Bearer "
    if not auth.startswith(prefix):
        return 403, "missing fleet key"
    if not hmac.compare_digest(auth[len(prefix) :].strip(), cfg.fleet.key.strip()):
        return 403, "invalid fleet key"
    return None


def rental_name(*, claimed: str | None, forwarded_for: str | None, remote_addr: str | None) -> str:
    """The name a config fetch is for.

    ``X-LLMAO-Host`` is the rental's name as its provider's provision.sh
    set it (a Vast instance id, a RunPod pod id; an address on a legacy
    machine), compared as given: a name is never normalized. Vast sits
    behind a transparent proxy, so the TCP peer is not the instance, and
    with the header present nothing else is consulted. Without it the
    rental is named by its address: the leftmost ``X-Forwarded-For`` (one
    reverse proxy; Werkzeug ProxyFix is WSGI and cannot wrap Quart's ASGI
    app), else the peer, normalized as addresses are.
    """
    name = (claimed or "").strip()
    if name:
        _LOGGER.info("%s=%r remote_addr=%r X-Forwarded-For=%r", HOST_HEADER, name, remote_addr, forwarded_for)
        return name
    if forwarded_for and forwarded_for.strip():
        name = normalize_peer_ip(forwarded_for.split(",")[0])
        _LOGGER.info("X-Forwarded-For=%r remote_addr=%r name=%s", forwarded_for, remote_addr, name)
        return name
    return normalize_peer_ip(remote_addr)


def server_json(intended: IntendedDeployment) -> edict:
    """One vLLM process as the rental installs it: the wire shape of ``servers[]``.

    ``model`` is the HF weights id, ``served_model_name`` what vLLM answers
    to, ``port`` the listen port (the public port is ours, not the rental's
    business; it binds inside the container), ``api_key`` the bearer the
    intent was minted with. A requirement the recipe does not declare is
    left out, so the installer's fit check knows it was not stated.
    """
    recipe = intended.recipe
    out = edict(
        name=intended.name,
        model=recipe.hf_model,
        served_model_name=recipe.served_name,
        host=intended.host,
        port=intended.listen_port,
        api_key=intended.api_key,
        args=list(recipe.args),
    )
    for field in ("gpu_memory_utilization", "max_model_len", "vram_gb", "disk_gb", "kv_bytes_per_token"):
        value = getattr(recipe, field)
        if value is not None:
            out[field] = value
    return out


def config_for_host(
    host: str,
    *,
    models: list,
    cfg: Any,
    approved: list[str] | None = None,
) -> edict:
    """The payload for one host: its IntendedDeployments as servers JSON.

    ``host`` is the name rental_name gave; ``models`` is the list loaded at
    startup. Raises intent.UnknownHostError when the host has neither rows
    nor an approval. Requires validate_fleet() already ran on cfg.
    """
    by_name = {str(model.model_name): model for model in models}
    servers = [server_json(i) for i in host_intents(cfg, by_name, host, approved=approved)]
    return edict(host=host, servers=servers)


@dataclass(frozen=True)
class ConfigFetch:
    """One name's config fetches: when first, when last, how many."""

    name: str
    first_at: float
    last_at: float
    count: int


class ProvisioningRecord:
    """Every name that fetched its config, intended or not: one ConfigFetch per name.

    Process memory only, lost on restart like the rest of the observed
    state. Names are recorded as rental_name gave them. Capped at
    FETCH_CAP names, the oldest last-seen going first; an intended name's
    fetch can go that way under a scan, which costs its "config fetched"
    age and nothing else, since the fleet copies the loading start it saw.
    """

    def __init__(self) -> None:
        self.fetches: dict[str, ConfigFetch] = {}

    def note(self, name: str, *, now: float | None = None) -> ConfigFetch:
        """The rental named ``name`` fetched its config."""
        stamp = now or time.time()
        seen = self.fetches.get(name)
        fetch = replace(seen, last_at=stamp, count=seen.count + 1) if seen else ConfigFetch(name, stamp, stamp, 1)
        self.fetches[name] = fetch
        extra = len(self.fetches) - FETCH_CAP
        if extra > 0:
            for old in sorted(self.fetches, key=lambda n: self.fetches[n].last_at)[:extra]:
                del self.fetches[old]
        return fetch

    def fetched_at(self, name: str) -> float | None:
        """When ``name`` last fetched its config; None if never."""
        fetch = self.fetches.get(name)
        return fetch and fetch.last_at

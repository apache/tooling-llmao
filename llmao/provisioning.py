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

"""The server side of a box provisioning itself.

A box provisions itself with one request: ``provision.sh`` runs,
``install_set.py`` fetches ``GET /vllm/config`` with the fleet key, and
the box installs what it gets (docs/box-protocol.md). This module is what
llmao keeps of that request. Today, the records:

- ``ConfigFetch``: the last fetch each known host made, and when. One of
  the six reconcile sources (docs/fleet-reconcile-design.md, Vocabulary);
  the Deployment reads it through its host as the Loading anchor.
- ``UnknownFetch``: a host nobody intended presenting the fleet key, with
  first seen, last seen and a count, so the /models attention strip can
  show it and an admin can approve it. Capped; the oldest last-seen go.

And the payload, ``config_for_host``: the host's IntendedDeployments
(llmao/intent.py) rendered as the servers JSON the box installs. It
derives nothing; the bearer and the recipe are on the record. How a box
names itself and the fleet-key check join from ``fleet.py`` and ``api.py``
next.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any

from easydict import EasyDict as edict  # noqa: N813

from llmao.intent import IntendedDeployment, host_intents
from llmao.models import load_models
from llmao.reconcile import normalize_peer_ip

# How many unknown hosts are remembered. A scan of the fleet key's port
# from strangers must not grow without bound; the oldest last-seen go.
UNKNOWN_FETCH_CAP = 50


def server_json(intended: IntendedDeployment) -> edict:
    """One vLLM process as the box installs it: the wire shape of ``servers[]``.

    ``model`` is the HF weights id, ``served_model_name`` what vLLM answers
    to, ``port`` the listen port (the public port is ours, not the box's
    business; it binds inside the container), ``api_key`` the bearer the
    intent was minted with. A requirement the recipe does not declare is
    left out, so the box's fit check knows it was not stated.
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
    models: list | None = None,
    cfg: Any = None,
    approved: list[str] | None = None,
) -> edict:
    """The payload for one host: its IntendedDeployments as servers JSON.

    Raises intent.UnknownHostError when the host has neither rows nor an
    approval. Requires validate_fleet() already ran on cfg.
    """
    models = models or load_models(cfg=cfg)
    by_name = {str(model.model_name): model for model in models}
    servers = [server_json(i) for i in host_intents(cfg, by_name, host, approved=approved)]
    return edict(host=normalize_peer_ip(host), servers=servers)


@dataclass(frozen=True)
class ConfigFetch:
    """A known host fetched its config at ``at``."""

    host: str
    at: float


@dataclass(frozen=True)
class UnknownFetch:
    """A host not in the fleet presented the fleet key: when first, when last, how often."""

    host: str
    first_seen: float
    last_seen: float
    count: int


class ProvisioningRecord:
    """What boxes have fetched: one ConfigFetch per known host, one UnknownFetch per stranger.

    Process memory only, lost on restart like the rest of the observed
    state. Hosts are given normalized (``normalize_peer_ip``); this record
    does not normalize.
    """

    def __init__(self) -> None:
        self.fetches: dict[str, ConfigFetch] = {}
        self.unknown: dict[str, UnknownFetch] = {}

    def note(self, host: str, *, now: float | None = None) -> ConfigFetch:
        """A known host fetched its config. Forgets it as a stranger, if it was one."""
        fetch = ConfigFetch(host, now or time.time())
        self.fetches[host] = fetch
        self.unknown.pop(host, None)
        return fetch

    def fetched_at(self, host: str) -> float | None:
        fetch = self.fetches.get(host)
        return fetch and fetch.at

    def note_unknown(self, host: str, *, now: float | None = None) -> list[str]:
        """A stranger presented the fleet key. Returns the hosts evicted by the cap, if any."""
        stamp = now or time.time()
        seen = self.unknown.get(host)
        if seen is None:
            self.unknown[host] = UnknownFetch(host, stamp, stamp, 1)
        else:
            self.unknown[host] = replace(seen, last_seen=stamp, count=seen.count + 1)
        extra = len(self.unknown) - UNKNOWN_FETCH_CAP
        if extra <= 0:
            return []
        oldest = sorted(self.unknown, key=lambda h: self.unknown[h].last_seen)[:extra]
        for old in oldest:
            del self.unknown[old]
        return oldest

    def forget_unknown(self, hosts) -> list[str]:
        """Drop strangers that are now members. Returns the hosts dropped."""
        dropped = [h for h in hosts if h in self.unknown]
        for host in dropped:
            del self.unknown[host]
        return dropped

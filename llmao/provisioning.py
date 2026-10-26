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

The endpoint, how a box names itself, the payload and the bearer
derivation join these from ``fleet.py`` in the next step.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

# How many unknown hosts are remembered. A scan of the fleet key's port
# from strangers must not grow without bound; the oldest last-seen go.
UNKNOWN_FETCH_CAP = 50


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

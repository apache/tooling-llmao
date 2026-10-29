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

"""Machines we rent, as their providers report them.

One of the six reconcile sources (docs/fleet-reconcile-design.md,
Vocabulary). A ``Rental`` is what one provider says about one machine:
nothing of ours is on it, and a new one replaces it on every fetch.
``ProviderRentals`` is one provider's set as of one fetch. The clients
(``vast_client``, ``runpod_client``) parse each provider's JSON into
Rentals; this module knows no provider's JSON.

The one piece of logic about rentals is ``match_rental``: which Rental a
host is on. By address first; failing that, the same machine it was on last time,
by provider and id. The second match is what keeps a server on a stopped
pod, which names no address, or on a restarted one under a new address.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from easydict import EasyDict as edict  # noqa: N813


@dataclass(frozen=True)
class Rental:
    """One provider's report on one machine.

    ``id`` is the provider's id for it (a Vast instance id, a RunPod pod
    id). ``host`` is its public address, "" while it initializes or once
    it has stopped. ``name`` is what a box on it sends as its host when it
    fetches its config (``X-LLMAO-Host``, set by that provider's
    provision.sh): its address on Vast, its pod id on RunPod. A
    ``fleet.hosts`` key, an approval and the bearer are all by name; the
    address is only ever dialed. ``status`` is the provider's own word, lower case
    (Vast's actual_status, RunPod's desiredStatus), "" when not given.
    ``ports`` maps a container listen port, as a string, to the public
    port it is published on; empty when the provider gives none.
    ``usd_per_hour`` is None when the provider does not say: unknown,
    not free. ``url`` is a console link, "" where the provider has no
    stable page per machine.
    """

    provider: str
    id: str
    label: str
    url: str
    usd_per_hour: float | None
    host: str
    name: str
    status: str
    ports: dict[str, int] = field(default_factory=edict)

    @property
    def running(self) -> bool:
        return self.status == "running"


@dataclass(frozen=True)
class ProviderRentals:
    """Every machine one provider reports we rent, as of ``fetched_at``.

    Keyed by the provider's id. An empty set is a real answer (nothing
    rented); the absence of a set, ``None`` on the fleet, is a source
    that is configured and not yet read, which concludes nothing.
    ``fetched_at`` is now unless a test says otherwise.
    """

    provider: str
    by_id: dict[str, Rental] = field(default_factory=edict)
    fetched_at: float = field(default_factory=time.time)

    def __iter__(self):
        return iter(self.by_id.values())


def match_rental(host: str, last: Rental | None, *sets: ProviderRentals | None) -> Rental | None:
    """The Rental a server is on, from the given provider sets, or None.

    ``host`` is the server's name, its ``fleet.hosts`` key. By the name the
    box sends first (a pod id, or an address); then by address, for a row
    keyed by the address of a box that sends something else; failing both,
    the machine ``last`` names, by provider and id, in whatever state and
    at whatever address it has now. An unread set (``None``) contributes
    nothing.
    """
    rentals: list[Rental] = [r for s in sets if s is not None for r in s]
    by_name = next((r for r in rentals if r.name and r.name == host), None)
    if by_name is not None:
        return by_name
    by_host = next((r for r in rentals if r.host and r.host == host), None)
    if by_host is not None:
        return by_host
    if last is None:
        return None
    return next((r for r in rentals if (r.provider, r.id) == (last.provider, last.id)), None)


def price(value: Any) -> float | None:
    """A provider's price field as a float, or None when it is not a number."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

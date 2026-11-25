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

from box import Box


@dataclass(frozen=True)
class Rental:
    """One provider's report on one machine.

    ``id`` is the provider's id for it (a Vast instance id, a RunPod pod
    id). ``host`` is its public address, "" while it initializes or once
    it has stopped. ``name`` is what a rental on it sends as its host when it
    fetches its config (``X-LLMAO-Host``, set by that provider's
    provision.sh): the provider's id for the machine, a Vast instance id
    or a RunPod pod id. A ``fleet.hosts`` key, an approval and the bearer
    are all by name; the address is only ever dialed. ``status`` is the provider's own word, lower case
    (Vast's actual_status, RunPod's status), "" when not given.
    ``ports`` maps a container listen port, as a string, to the public
    port it is published on; empty when the provider gives none.
    ``usd_per_hour`` is None when the provider does not say: unknown,
    not free. ``url`` is a console link, "" where the provider has no
    stable page per machine.

    The hardware is what the provider lists, fixed for the life of the
    machine: ``gpu`` is the card as the provider names it, ``gpus`` how
    many, ``vram_gb`` the memory of one card, ``disk_gb`` the disk
    (Vast's one allotment, or a RunPod pod's container disk), ``volume_gb``
    a RunPod pod's volume, where its weights live (Vast has no second
    disk), ``disk_used_gb`` what is used, which Vast reports and RunPod
    does not. Each is "" / 0 / None where the provider does not say. These
    are the actual figures, where a model's ``vram_gb`` and ``disk_gb`` in
    models.yaml are what it needs, typed by hand and wrong before now.
    """

    provider: str
    id: str
    label: str
    url: str
    usd_per_hour: float | None
    host: str
    name: str
    status: str
    ports: dict[str, int] = field(default_factory=Box)
    gpu: str = ""
    gpus: int = 0
    vram_gb: float | None = None
    disk_gb: float | None = None
    volume_gb: float | None = None
    disk_used_gb: float | None = None

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def hardware(self) -> str:
        """The machine in one line, for an admin sizing a rental: "RTX 3090 x4, 24 GB each, 40 GB disk, 0 used".

        A RunPod pod's two disks read "5 GB disk + 60 GB volume". Only what
        the provider said is in it; "" when it said nothing.
        """
        parts = []
        if self.gpu:
            parts.append(f"{self.gpu} x{self.gpus}" if self.gpus else self.gpu)
        if self.vram_gb is not None:
            parts.append(f"{self.vram_gb:g} GB each" if self.gpus > 1 else f"{self.vram_gb:g} GB")
        if self.disk_gb is not None:
            disk = f"{self.disk_gb:g} GB disk"
            if self.volume_gb is not None:
                disk += f" + {self.volume_gb:g} GB volume"
            if self.disk_used_gb is not None:
                disk += f", {self.disk_used_gb:g} used"
            parts.append(disk)
        return ", ".join(parts)

    @property
    def ident(self) -> str:
        """How a page or a log line names the machine: the provider and its id, "Vast 53865410".

        The one name that survives what a provider does to a machine: an
        address changes when it is moved or restarted, and goes when the
        rental ends. The label (its GPUs, a pod name), status and console
        link are facts a page shows beside the name, not part of it.
        """
        return f"{self.provider} {self.id}"


@dataclass(frozen=True)
class ProviderRentals:
    """Every machine one provider reports we rent, as of ``fetched_at``.

    Keyed by the provider's id. An empty set is a real answer (nothing
    rented); the absence of a set, ``None`` on the fleet, is a source
    that is configured and not yet read, which concludes nothing.
    ``fetched_at`` is now unless a test says otherwise.
    """

    provider: str
    by_id: dict[str, Rental] = field(default_factory=Box)
    fetched_at: float = field(default_factory=time.time)

    def __iter__(self):
        return iter(self.by_id.values())


def match_rental(host: str, last: Rental | None, *sets: ProviderRentals | None) -> Rental | None:
    """The Rental a server is on, from the given provider sets, or None.

    ``host`` is the server's name, its ``fleet.hosts`` key. By the name the
    rental sends first (the provider's id); then by address, for a row keyed
    by the address of a rental provisioned before names were ids; failing both,
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

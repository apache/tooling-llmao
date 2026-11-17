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
"""Where each machine is and what it rents for: Vast and RunPod Rentals, onto servers and capacity rows."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
import pytest
from easydict import EasyDict as edict  # noqa: N813

import llmao.fleet
import llmao.runpod_client
from llmao.capacity import HOUR_S, HourlyRollup
from llmao.fleet import UNLISTED_PASSES, Fleet, VllmServer
from llmao.rental import ProviderRentals, Rental, match_rental
from llmao.runpod_client import fetch_hardware, rentals_from_pods
from llmao.sampler import MetricsSampler
from llmao.vast_client import VAST_CONSOLE_URL, rentals_from_body


def _server(host, port=8001, *, public_port=None):
    return VllmServer(
        model_name="qwen3-8b",
        name=f"qwen3-8b-{port}",
        host=host,
        listen_port=port,
        hf_model="Qwen/Qwen3-8B",
        api_key="sk-x",
        args=[],
        public_port=public_port,
    )


def _rental(price, provider="Vast", id="1", label="GB10 x1", host="", status="running", ports=None):
    return Rental(
        provider=provider,
        id=id,
        label=label,
        url="",
        usd_per_hour=price,
        host=host,
        # What the rental sends as its host: the provider's id for the machine.
        name=id,
        status=status,
        ports=edict(ports or {}),
    )


def _reported(*rentals, provider=None):
    """One provider's set, as of now: what it reports we rent."""
    provider = provider or (rentals[0].provider if rentals else "Vast")
    return ProviderRentals(provider, edict({r.id: r for r in rentals}))


def test_a_vast_rental_is_its_instance_gpus_status_address_and_price():
    body = {
        "instances": [
            {
                "id": 55406468,
                "public_ipaddr": "203.0.113.10",
                "gpu_name": "GB10",
                "num_gpus": 1,
                "dph_total": 0.2059,
                "actual_status": "running",
                "ports": {"8001/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41234"}]},
                # The hardware as the host reports it: one card's memory in
                # MB, the one disk in GB with Vast's float noise, and what
                # is used of it.
                "gpu_ram": 131072,
                "disk_space": 79.99999999999997,
                "disk_usage": 37.0,
            },
            {"id": 53865410, "public_ipaddr": "198.51.100.8", "gpu_name": "RTX 3060", "num_gpus": 2, "gpu_ram": 12288},
            {"id": 1, "public_ipaddr": "", "dph_total": 0.5, "actual_status": "loading"},
        ]
    }
    rentals = rentals_from_body(body)
    assert rentals["55406468"] == Rental(
        provider="Vast",
        id="55406468",
        label="GB10 x1",
        url=VAST_CONSOLE_URL,
        usd_per_hour=0.2059,
        host="203.0.113.10",
        name="55406468",
        status="running",
        ports={"8001": 41234},
        gpu="GB10",
        gpus=1,
        vram_gb=128.0,
        disk_gb=80.0,
        disk_used_gb=37.0,
    )
    assert rentals["55406468"].hardware == "GB10 x1, 128 GB, 80 GB disk, 37 used"
    # Listed, but no price: unknown, not free. No status given: unknown, not running. No ports: none.
    assert rentals["53865410"].label == "RTX 3060 x2"
    assert rentals["53865410"].usd_per_hour is None
    assert rentals["53865410"].status == ""
    assert rentals["53865410"].running is False
    assert rentals["53865410"].ports == {}
    # Two cards: the memory is each card's. No disk figures: none said.
    assert rentals["53865410"].hardware == "RTX 3060 x2, 12 GB each"
    assert rentals["1"].hardware == ""
    # Listed, no address yet: kept, with its status, so it is not a miss.
    assert (rentals["1"].host, rentals["1"].status) == ("", "loading")
    assert set(rentals) == {"55406468", "53865410", "1"}
    assert rentals_from_body({}) == {}


def test_every_runpod_pod_is_kept_with_its_status_and_address():
    pods = [
        {
            "id": "tafo5",
            "name": "llmao-qwen38-27b",
            "status": "RUNNING",
            "cost": 2.49,
            "ports": ["8002/tcp", "22/tcp", "8003/tcp"],
            # The hardware: the card by type id and count (gpu.memory is
            # the pod's system RAM, not VRAM, and is not read), the
            # container disk, and the volume at /workspace.
            "gpu": {"count": 1, "id": "NVIDIA RTX PRO 6000 Blackwell Server Edition", "memory": 188, "vcpuCount": 16},
            "disk": 30,
            "mounts": {"persistent": {"path": "/workspace", "size": 80}},
            # The live port map, as fetch_pods merges it from the pod's own
            # GET. The http entry is RunPod's own port on the proxy's
            # address, first in the list as it often is, skipped for both
            # the port and the IP; an entry that is not a port pair is
            # dropped; the IP is the TCP entries'.
            "runtime": {
                "uptime": 918889,
                "ports": [
                    {"private": 19123, "public": 60569, "type": "http", "ip": "198.51.100.7"},
                    {"private": 8002, "public": 16643, "type": "tcp", "ip": "203.0.113.20"},
                    {"private": "22", "public": "10341", "type": "tcp", "ip": "203.0.113.20"},
                    {"private": 8003, "public": None, "type": "tcp", "ip": "203.0.113.20"},
                ],
            },
        },
        # A stopped pod has no map and still reports a price: listed, not
        # running. Its volume is a network volume, named by id: the size
        # comes from the lookup.
        {
            "id": "tjfs5",
            "name": "testing",
            "status": "EXITED",
            "cost": 0.89,
            "runtime": None,
            "gpu": {"count": 2, "id": "NVIDIA A100 80GB PCIe"},
            "disk": 5,
            "mounts": {"network": [{"path": "/workspace", "volumeId": "vol1"}]},
        },
        # Just started: RUNNING, and no map yet, so no address.
        {"id": "x", "status": "RUNNING", "cost": "0.89", "runtime": None},
        {"name": "no id", "status": "RUNNING"},
    ]
    # What fetch_hardware looked up: the catalog's short name and VRAM per
    # card type id, and the network volume's size.
    hardware = edict(
        cards_by_id={
            "NVIDIA RTX PRO 6000 Blackwell Server Edition": edict(name="RTX PRO 6000", vram_gb=96.0),
            "NVIDIA A100 80GB PCIe": edict(name="A100 PCIe", vram_gb=80.0),
        },
        volume_gb_by_id={"vol1": 200.0},
    )
    assert rentals_from_pods(pods, hardware=hardware) == {
        "tafo5": Rental(
            provider="RunPod",
            id="tafo5",
            label="llmao-qwen38-27b",
            url="",
            usd_per_hour=2.49,
            host="203.0.113.20",
            name="tafo5",
            status="running",
            ports={"8002": 16643, "22": 10341},
            gpu="RTX PRO 6000",
            gpus=1,
            vram_gb=96.0,
            disk_gb=30.0,
            volume_gb=80.0,
        ),
        "tjfs5": Rental(
            provider="RunPod",
            id="tjfs5",
            label="testing",
            url="",
            usd_per_hour=0.89,
            host="",
            name="tjfs5",
            status="exited",
            gpu="A100 PCIe",
            gpus=2,
            vram_gb=80.0,
            disk_gb=5.0,
            volume_gb=200.0,
        ),
        "x": Rental(
            provider="RunPod", id="x", label="", url="", usd_per_hour=0.89, host="", name="x", status="running"
        ),
    }
    rentals = rentals_from_pods(pods, hardware=hardware)
    assert rentals["tafo5"].hardware == "RTX PRO 6000 x1, 96 GB, 30 GB disk + 80 GB volume"
    assert rentals["tjfs5"].hardware == "A100 PCIe x2, 80 GB each, 5 GB disk + 200 GB volume"
    # Without the lookups the card is its type id and the disks are still
    # read; the VRAM and the network volume are unknown.
    assert rentals_from_pods(pods)["tjfs5"].hardware == "NVIDIA A100 80GB PCIe x2, 5 GB disk"
    assert rentals_from_pods({"error": "nope"}) == {}


def test_a_card_type_and_a_network_volume_are_looked_up_once_per_process(monkeypatch):
    """The second read names the same card type and volume: no request. Their sizes never change."""
    monkeypatch.setattr(llmao.runpod_client, "_CARD_BY_ID", {})
    monkeypatch.setattr(llmao.runpod_client, "_VOLUME_GB_BY_ID", {})
    requests = []

    def handle(request):
        # raw_path is what went on the wire: the card type's spaces quoted.
        requests.append(request.url.raw_path.decode())
        if request.url.path == "/v2/catalog/gpus/NVIDIA GeForce RTX 4090":
            return httpx.Response(200, json={"id": "NVIDIA GeForce RTX 4090", "memory": 24, "name": "RTX 4090"})
        assert request.url.path == "/v2/network-volumes/vol1"
        return httpx.Response(200, json={"id": "vol1", "size": 150})

    pods = [
        {"id": "a", "gpu": {"count": 1, "id": "NVIDIA GeForce RTX 4090"}, "mounts": {"persistent": {"size": 20}}},
        {
            "id": "b",
            "gpu": {"count": 1, "id": "NVIDIA GeForce RTX 4090"},
            "mounts": {"network": [{"volumeId": "vol1"}]},
        },
        {"id": "c", "status": "EXITED"},
    ]

    async def twice():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            first = await fetch_hardware("rp", pods, client=client)
            second = await fetch_hardware("rp", pods, client=client)
        return first, second

    first, second = asyncio.run(twice())
    assert (
        first
        == second
        == {
            "cards_by_id": {"NVIDIA GeForce RTX 4090": {"name": "RTX 4090", "vram_gb": 24.0}},
            "volume_gb_by_id": {"vol1": 150.0},
        }
    )
    assert sorted(requests) == ["/v2/catalog/gpus/NVIDIA%20GeForce%20RTX%204090", "/v2/network-volumes/vol1"]


def test_a_provider_set_is_stamped_when_made_and_iterates_its_rentals():
    before = time.time()
    reported = _reported(_rental(1.0, id="a"), _rental(2.0, id="b"))
    assert reported.provider == "Vast"
    assert before <= reported.fetched_at <= time.time()
    assert [r.id for r in reported] == ["a", "b"]
    assert ProviderRentals("RunPod", fetched_at=5.0).fetched_at == 5.0


def test_a_host_matches_its_rental_by_address_then_by_the_machine_it_was_on():
    running = _rental(2.49, provider="RunPod", id="p", host="203.0.113.20")
    stopped = _rental(0.89, provider="RunPod", id="p", host="", status="exited")
    other = _rental(1.0, provider="RunPod", id="q", host="203.0.113.21")
    assert match_rental("203.0.113.20", None, _reported(running, other)) == running
    # Nothing names the address and nothing was matched before: no Rental.
    assert match_rental("203.0.113.20", None, _reported(stopped, other)) is None
    # The same machine, now stopped and without an address, is still the one.
    assert match_rental("203.0.113.20", running, _reported(stopped, other)) == stopped
    # An unread provider contributes nothing; an empty one is a real answer.
    assert match_rental("203.0.113.20", running, None, _reported(provider="RunPod")) is None
    # The address wins over the old machine when another has taken it.
    assert match_rental("203.0.113.21", running, _reported(stopped, other)) == other


def test_a_runpod_rental_is_dialed_on_the_port_its_provider_maps(caplog):
    """The provider's report is the dial address for a RunPod machine as for a Vast one; a stopped pod keeps the last port."""
    srv = _server("203.0.113.20", 8002)
    fleet = _fleet_with_runpod([srv])
    assert srv.public_port is None
    fleet.runpod_rentals = _reported(
        _rental(2.49, provider="RunPod", id="p", host="203.0.113.20", ports={"8002": 16643})
    )
    with caplog.at_level("INFO", logger="llmao.fleet"):
        fleet.place_servers()
    assert srv.public_port == 16643
    assert srv.api_base == "http://203.0.113.20:16643/v1"
    assert "runpod: qwen3-8b on 203.0.113.20:8002 public port 16643" in caplog.text
    # Stopped: no map. The last port stands, and nothing is said.
    caplog.clear()
    fleet.runpod_rentals = _reported(_rental(0.89, provider="RunPod", id="p", host="", status="exited"))
    with caplog.at_level("INFO", logger="llmao.fleet"):
        fleet.place_servers()
    assert srv.public_port == 16643
    assert "public port" not in caplog.text
    # Recreated: RunPod hands out a new port, and the report is believed.
    fleet.runpod_rentals = _reported(
        _rental(2.49, provider="RunPod", id="p", host="203.0.113.20", ports={"8002": 17001})
    )
    fleet.place_servers()
    assert srv.public_port == 17001
    # A map that knows the machine but not this listen port is a warning.
    fleet.runpod_rentals = _reported(_rental(2.49, provider="RunPod", id="p", host="203.0.113.20", ports={"22": 10341}))
    with caplog.at_level("WARNING", logger="llmao.fleet"):
        fleet.place_servers()
    assert "runpod: no public port for qwen3-8b on RunPod p:8002" in caplog.text
    assert srv.public_port == 17001


def test_a_rental_lands_on_every_server_on_it():
    a, b, other = _server("203.0.113.10", 8001), _server("203.0.113.10", 8002), _server("198.51.100.8")
    fleet = Fleet(None, [a, b, other])
    placed = fleet.apply_rentals(_reported(_rental(0.206, host="203.0.113.10")))
    assert placed == [a, b]
    assert (a.rental_usd_per_hour, b.rental_usd_per_hour, other.rental_usd_per_hour) == (0.206, 0.206, None)
    assert a.rental.label == "GB10 x1"
    assert other.rental is None


def test_a_reported_machine_with_no_address_is_not_placed_until_it_has_one():
    """An initializing pod names no public IP, so no server can be its yet."""
    srv = _server("203.0.113.20")
    fleet = Fleet(None, [srv])
    assert fleet.apply_rentals(_reported(_rental(2.49, provider="RunPod", id="p", host="", status="running"))) == []
    assert srv.rental is None


def test_a_stopped_pod_keeps_its_server_listed_and_says_so(caplog):
    """Listed, not running: a stopped pod drops its address; it is not a miss for the unlisted count."""
    srv = _server("203.0.113.20")
    fleet = _fleet_with_runpod([srv])
    running = _rental(2.49, provider="RunPod", id="p", host="203.0.113.20")
    fleet.runpod_rentals = _reported(running)
    fleet.place_servers()
    assert srv.rental == running
    stopped = _rental(0.89, provider="RunPod", id="p", host="", status="exited")
    fleet.runpod_rentals = _reported(stopped)
    with caplog.at_level("INFO", logger="llmao.fleet"):
        for _ in range(UNLISTED_PASSES + 1):
            fleet.place_servers()
    assert srv.unlisted is False
    assert srv.unlisted_passes == 0
    assert srv.rental == stopped, "the server follows its pod, status and price included"
    assert caplog.text.count("exited at no address") == 1
    # Started again under a new address: still the same pod, so still listed and placed.
    moved = _rental(2.49, provider="RunPod", id="p", host="203.0.113.21")
    fleet.runpod_rentals = _reported(moved)
    fleet.place_servers()
    assert srv.rental == moved
    assert srv.unlisted is False


def test_the_address_match_wins_over_the_old_pod():
    """A server whose address another machine now has is on that machine; the old pod's row is history."""
    srv = _server("203.0.113.20")
    srv.rental = _rental(2.49, provider="RunPod", id="old", host="")
    fleet = _fleet_with_runpod([srv])
    new = _rental(1.0, provider="RunPod", id="new", host="203.0.113.20")
    fleet.runpod_rentals = _reported(_rental(2.49, provider="RunPod", id="old", host="", status="exited"), new)
    fleet.place_servers()
    assert srv.rental == new


def _fleet_with_runpod(servers):
    return Fleet(edict(providers=edict(runpod=edict(api_key="rp-test"))), servers)


def _runpod_v2(ip, price=2.49):
    """A MockTransport handler: RunPod's v2 list (no runtime) and the one pod's GET (with it)."""

    def handle(request):
        pod = {"id": "p", "name": "n", "status": "RUNNING", "cost": price, "runtime": None}
        if request.url.path == "/v2/pods":
            return httpx.Response(200, json={"pods": [pod], "pagination": {"hasNextPage": False, "nextCursor": None}})
        assert request.url.path == "/v2/pods/p"
        pod["runtime"] = {"ports": [{"private": 8001, "public": 16643, "type": "tcp", "ip": ip}]}
        return httpx.Response(200, json=pod)

    return handle


def test_the_runpod_report_is_read_into_the_fleet_and_placed_on_the_next_pass():
    srv = _server("203.0.113.20")
    fleet = _fleet_with_runpod([srv])

    async def read():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_runpod_v2("203.0.113.20"))) as client:
            await fleet.read_runpod_rentals(client)

    asyncio.run(read())
    assert fleet.runpod_rentals.provider == "RunPod"
    assert fleet.runpod_rentals.by_id["p"].host == "203.0.113.20", "the address comes from the pod's own GET"
    assert srv.rental is None, "the runner only reads; the lifecycle places"
    fleet.place_servers()
    assert srv.rental_usd_per_hour == 2.49
    assert srv.public_port == 16643
    assert srv.unlisted is False


def test_without_a_provider_key_the_runner_ends_at_once():
    fleet = Fleet(edict(), [_server("203.0.113.20")])
    asyncio.run(asyncio.wait_for(fleet.run_rentals(), timeout=1))
    assert fleet.rentals_read.is_set(), "the lifecycle's first pass must not wait on a runner that has nothing to read"


def test_the_runner_reads_both_providers_on_its_first_pass():
    """The runner calls each reader by its name: a rename once left it calling a method that did not exist,
    and every pass failed with a warning while the pods went unread."""
    fleet = _fleet_renting_from_both([_server("203.0.113.20")])
    calls = []

    async def read_vast(client):
        calls.append("Vast")

    async def read_runpod(client):
        calls.append("RunPod")
        raise asyncio.CancelledError

    fleet.read_vast_rentals = read_vast
    fleet.read_runpod_rentals = read_runpod
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fleet.run_rentals())
    assert calls == ["Vast", "RunPod"]
    assert fleet.rentals_read.is_set()


def test_one_provider_failing_to_answer_leaves_the_other_read(caplog):
    fleet = _fleet_renting_from_both([_server("203.0.113.20")])

    async def read_vast(client):
        raise httpx.ConnectError("vast is down")

    async def read_runpod(client):
        fleet.runpod_rentals = _reported()

    fleet.read_vast_rentals = read_vast
    fleet.read_runpod_rentals = read_runpod
    with caplog.at_level(logging.WARNING):
        asyncio.run(fleet.read_rentals(None))
    assert fleet.vast_rentals is None, "the outage leaves the last set, here none"
    assert fleet.runpod_rentals is not None
    assert "the Vast rentals could not be read" in caplog.text


def test_the_first_lifecycle_pass_waits_for_the_first_listing_read(monkeypatch):
    """At startup the runner's first read lands before the first probe, so a server keyed by a
    provider's id has its address; otherwise it read as unreached for one health interval."""
    fleet = _fleet_renting_from_both([_server("203.0.113.20")])
    # The whole pass runs, so the knobs every step reads are here. The probe
    # ends the lifecycle; a step that raised before it would be caught and
    # the loop would spin at health_interval_s, so that is not zero.
    fleet.cfg.fleet = edict(
        hosts={}, health_interval_s=1, health_timeout_s=1, health_grace_s=60, health_fail_threshold=3
    )
    order = []

    async def read_rentals(client):
        order.append("read")
        fleet.vast_rentals = _reported()
        fleet.runpod_rentals = _reported()

    async def probe_all(*, client=None, now=None):
        order.append("probe")
        raise asyncio.CancelledError

    fleet.read_rentals = read_rentals
    fleet.probe_all = probe_all
    monkeypatch.setattr(llmao.fleet, "RENTALS_INTERVAL_S", 3600)

    async def both():
        rentals = asyncio.create_task(fleet.run_rentals())
        try:
            await fleet.run_lifecycle()
        finally:
            rentals.cancel()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(both())
    assert order == ["read", "probe"]


def _fleet_renting_from_both(servers):
    cfg = edict(providers=edict(vast=edict(api_key="v"), runpod=edict(api_key="rp")))
    return Fleet(cfg, servers)


def test_a_host_no_provider_reports_is_unlisted_and_said_once(caplog):
    gone, here = _server("203.0.113.30"), _server("203.0.113.20")
    gone.rental = _rental(0.22)
    fleet = _fleet_renting_from_both([gone, here])
    fleet.vast_rentals = _reported()
    fleet.runpod_rentals = _reported(_rental(2.49, provider="RunPod", id="p", host="203.0.113.20"))
    with caplog.at_level("INFO", logger="llmao.fleet"):
        for _ in range(UNLISTED_PASSES + 1):
            fleet.place_servers()
    assert (gone.unlisted, here.unlisted) == (True, False)
    # Its old machine and price no longer apply.
    assert gone.rental is None
    assert caplog.text.count("no provider lists 203.0.113.30") == 1
    # Reported again: said once, and placed.
    fleet.vast_rentals = _reported(_rental(0.22, host="203.0.113.30"))
    with caplog.at_level("INFO", logger="llmao.fleet"):
        fleet.place_servers()
    assert gone.unlisted is False
    assert "203.0.113.30 is listed again" in caplog.text


def test_a_host_no_listing_has_ever_named_is_unlisted_once_the_passes_run():
    """A rental that ended while llmao was down, read after the restart.

    No listing in this process has ever named it, and that is no guard: an
    unconfigured provider is not a missing report, so `complete` goes true
    as soon as Vast answers, and the count runs from the first pass. The
    verdict is a sentence; nothing Healthy is deleted on it.
    """
    never = _server("203.0.113.40")
    fleet = Fleet(edict(providers=edict(vast=edict(api_key="v"))), [never])
    fleet.vast_rentals = _reported()
    for _ in range(UNLISTED_PASSES - 1):
        fleet.place_servers()
    assert never.unlisted is False
    fleet.place_servers()
    assert never.unlisted is True


def test_a_provisioning_gap_shorter_than_the_threshold_is_not_a_dead_rental():
    """The production incident: a RunPod pod absent while it provisions.

    A pod was missing from the pod list for over 90 minutes after a
    recreate, then appeared as RUNNING. One absent report must not conclude
    the rental ended, because the route gets deleted and re-added on
    alternating passes for as long as the gap lasts.
    """
    pod = _server("203.0.113.50")
    fleet = _fleet_renting_from_both([pod])
    reported = _reported(_rental(2.49, provider="RunPod", id="p", host="203.0.113.50"))
    fleet.vast_rentals, fleet.runpod_rentals = _reported(), reported
    fleet.place_servers()
    assert pod.unlisted is False
    # Absent, but for fewer passes than the threshold.
    fleet.runpod_rentals = _reported(provider="RunPod")
    for _ in range(UNLISTED_PASSES - 1):
        fleet.place_servers()
    assert pod.unlisted is False, "a provisioning gap is not a dead rental"
    # Back in the report: the count resets, so a flapping host never flips.
    fleet.runpod_rentals = reported
    fleet.place_servers()
    assert pod.unlisted is False
    fleet.runpod_rentals = _reported(provider="RunPod")
    for _ in range(UNLISTED_PASSES - 1):
        fleet.place_servers()
    assert pod.unlisted is False, "the count must reset on a sighting"


def test_nothing_is_concluded_until_every_provider_has_answered():
    srv = _server("203.0.113.30")
    fleet = _fleet_renting_from_both([srv])
    fleet.vast_rentals = _reported()  # Vast answered; RunPod not yet
    fleet.place_servers()
    assert srv.unlisted is False
    # A fleet that rents from nobody (a local one) has nothing unlisted.
    local = Fleet(edict(), [_server("127.0.0.1")])
    local.place_servers()
    assert local.servers[0].unlisted is False


def test_a_stale_listing_concludes_nothing_about_the_hosts_it_lacks():
    """RunPod's v1 API was retired and the last report stood for a day; a host absent from a report that old
    is not absent, it is unheard of. The report still places the hosts it has: the machines have not moved."""
    on_old = _server("203.0.113.30")
    absent = _server("203.0.113.31")
    fleet = _fleet_renting_from_both([on_old, absent])
    old = time.time() - llmao.fleet.STALE_LISTING_PASSES * llmao.fleet.RENTALS_INTERVAL_S - 1
    fleet.vast_rentals = _reported()
    fleet.runpod_rentals = ProviderRentals(
        "RunPod", edict(p=_rental(2.49, provider="RunPod", id="p", host="203.0.113.30")), fetched_at=old
    )
    for _ in range(UNLISTED_PASSES + 1):
        fleet.place_servers()
    assert on_old.rental is not None and on_old.rental.id == "p"
    assert absent.unlisted is False and absent.unlisted_passes == 0


def test_listings_say_each_providers_age_staleness_and_last_error(caplog):
    now = 1_000_000.0
    fleet = _fleet_renting_from_both([_server("203.0.113.30")])
    # Configured, never read: stale, "never".
    assert [(li.provider, li.ago, li.stale, li.error) for li in fleet.listings(now=now)] == [
        ("Vast", "never", True, ""),
        ("RunPod", "never", True, ""),
    ]
    fleet.vast_rentals = ProviderRentals("Vast", fetched_at=now - 32)
    limit = llmao.fleet.STALE_LISTING_PASSES * llmao.fleet.RENTALS_INTERVAL_S
    fleet.runpod_rentals = ProviderRentals("RunPod", fetched_at=now - limit - 1)
    vast, runpod = fleet.listings(now=now)
    assert (vast.ago, vast.stale) == ("32s ago", False)
    assert runpod.stale is True
    # A read that fails leaves its reason beside the age, logged once per run of failures; a read that
    # answers clears it.
    calls = []

    async def read_vast(client):
        calls.append("Vast")

    async def read_runpod(client):
        raise httpx.ConnectError("name resolution failed")

    fleet.read_vast_rentals = read_vast
    fleet.read_runpod_rentals = read_runpod
    with caplog.at_level(logging.WARNING):
        asyncio.run(fleet.read_rentals(None))
        asyncio.run(fleet.read_rentals(None))
    assert caplog.text.count("the RunPod rentals could not be read") == 1
    assert fleet.listings(now=now)[1].error == "ConnectError: name resolution failed"

    async def read_runpod_ok(client):
        fleet.runpod_rentals = ProviderRentals("RunPod", fetched_at=now)

    fleet.read_runpod_rentals = read_runpod_ok
    asyncio.run(fleet.read_rentals(None))
    assert fleet.listings(now=now)[1] == edict(provider="RunPod", ago="0s ago", stale=False, error="")
    # A fleet that rents from nobody lists nothing.
    assert Fleet(edict(), []).listings() == []


def test_each_row_carries_its_servers_share_of_the_machine():
    """Two servers on a $0.206 machine are $0.103 each, so adding rows up gives the bill."""
    a = _server("203.0.113.10", 8001, public_port=8001)
    b = _server("203.0.113.10", 8002, public_port=8002)
    unpriced = _server("198.51.100.8", public_port=8001)
    for srv in (a, b):
        srv.rental = _rental(0.206)
    rollup = HourlyRollup(15)
    sampler = MetricsSampler(Fleet(None, [a, b, unpriced]), rollup, interval_s=15)

    async def one_pass():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, text=""))
        async with httpx.AsyncClient(transport=transport) as client:
            await sampler.sample_once(client)

    asyncio.run(one_pass())
    rows = {h.server: h.row(15) for h in rollup.take_finished(time.time() + HOUR_S)}
    assert {s: r.usd_per_hour for s, r in rows.items()} == {
        "203.0.113.10:8001": 0.103,
        "203.0.113.10:8002": 0.103,
        "198.51.100.8:8001": None,
    }
    # Each row names the machine too, so the hours of one rental stay
    # together when its address changes and after it ends.
    assert {s: (r.rental, r.rental_label) for s, r in rows.items()} == {
        "203.0.113.10:8001": ("Vast 1", "GB10 x1"),
        "203.0.113.10:8002": ("Vast 1", "GB10 x1"),
        "198.51.100.8:8001": ("", ""),
    }

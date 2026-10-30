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
import time

import httpx
import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.capacity import HOUR_S, HourlyRollup
from llmao.fleet import UNLISTED_PASSES, Fleet, VllmServer
from llmao.rental import ProviderRentals, Rental, match_rental
from llmao.runpod_client import rentals_from_pods
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
        # What the box sends as its host: the provider's id for the machine.
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
            },
            {"id": 53865410, "public_ipaddr": "198.51.100.8", "gpu_name": "RTX 3060", "num_gpus": 2},
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
    )
    # Listed, but no price: unknown, not free. No status given: unknown, not running. No ports: none.
    assert rentals["53865410"].label == "RTX 3060 x2"
    assert rentals["53865410"].usd_per_hour is None
    assert rentals["53865410"].status == ""
    assert rentals["53865410"].running is False
    assert rentals["53865410"].ports == {}
    # Listed, no address yet: kept, with its status, so it is not a miss.
    assert (rentals["1"].host, rentals["1"].status) == ("", "loading")
    assert set(rentals) == {"55406468", "53865410", "1"}
    assert rentals_from_body({}) == {}


def test_every_runpod_pod_is_kept_with_its_status_and_address():
    pods = [
        {
            "id": "tafo5",
            "name": "llmao-qwen38-27b",
            "desiredStatus": "RUNNING",
            "publicIp": "203.0.113.20",
            "costPerHr": 2.49,
            # Container port → public port. "22/tcp" is the shape of the pod's
            # `ports` field, tolerated here; a value that is not a port is dropped.
            "portMappings": {"8002": 16643, "22/tcp": "10341", "8003": None},
        },
        # A stopped pod has no address and still reports a price: listed, not running.
        {"id": "tjfs5", "name": "testing", "desiredStatus": "EXITED", "publicIp": "", "costPerHr": 0.89},
        # Initializing: RUNNING with no address yet.
        {"id": "x", "desiredStatus": "RUNNING", "publicIp": None, "costPerHr": "0.89", "portMappings": None},
        {"name": "no id", "desiredStatus": "RUNNING"},
    ]
    assert rentals_from_pods(pods) == {
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
        ),
        "x": Rental(
            provider="RunPod", id="x", label="", url="", usd_per_hour=0.89, host="", name="x", status="running"
        ),
    }
    assert rentals_from_pods({"error": "nope"}) == {}


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
    assert "runpod: qwen3-8b-8002@203.0.113.20 listen 8002 public 16643" in caplog.text
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
    assert "runpod: no public port for qwen3-8b-8002@203.0.113.20 listen 8002" in caplog.text
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


def _pod(ip, price=2.49):
    return {"id": "p", "name": "n", "desiredStatus": "RUNNING", "publicIp": ip, "costPerHr": price}


def test_the_runpod_report_is_read_into_the_fleet_and_placed_on_the_next_pass():
    srv = _server("203.0.113.20")
    fleet = _fleet_with_runpod([srv])

    async def read():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[_pod("203.0.113.20")]))
        async with httpx.AsyncClient(transport=transport) as client:
            await fleet.read_runpod_rentals(client)

    asyncio.run(read())
    assert fleet.runpod_rentals.provider == "RunPod"
    assert srv.rental is None, "the runner only reads; the lifecycle places"
    fleet.place_servers()
    assert srv.rental_usd_per_hour == 2.49
    assert srv.unlisted is False


def test_without_a_runpod_key_the_runner_ends_at_once():
    fleet = Fleet(edict(), [_server("203.0.113.20")])
    asyncio.run(asyncio.wait_for(fleet.run_runpod_rentals(), timeout=1))


def test_the_runner_reads_the_rentals_on_its_first_pass():
    """The runner calls the reader by its name: a rename once left it calling a method that did not exist,
    and every pass failed with a warning while the pods went unread."""
    fleet = _fleet_with_runpod([_server("203.0.113.20")])
    calls = []

    async def read(client):
        calls.append(client)
        raise asyncio.CancelledError

    fleet.read_runpod_rentals = read
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fleet.run_runpod_rentals())
    assert len(calls) == 1


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
    hours = {h.server: h.row(15).usd_per_hour for h in rollup.take_finished(time.time() + HOUR_S)}
    assert hours == {"203.0.113.10:8001": 0.103, "203.0.113.10:8002": 0.103, "198.51.100.8:8001": None}

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
"""Where each box is and what it rents for: Vast and RunPod listings, onto servers and capacity rows."""

from __future__ import annotations

import asyncio
import time

import httpx
from easydict import EasyDict as edict  # noqa: N813

from llmao.capacity import HOUR_S, HourlyRollup
from llmao.fleet import UNLISTED_PASSES, Fleet, VllmServer
from llmao.runpod_client import boxes_from_pods
from llmao.sampler import MetricsSampler
from llmao.vast_client import VAST_CONSOLE_URL, boxes_from_body


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


def _box(price, provider="Vast", box_id="1", label="GB10 x1"):
    return edict(provider=provider, box_id=box_id, label=label, url="", usd_per_hour=price)


def test_a_vast_box_is_its_instance_gpus_and_price_by_public_ip():
    body = {
        "instances": [
            {"id": 55406468, "public_ipaddr": "203.0.113.10", "gpu_name": "GB10", "num_gpus": 1, "dph_total": 0.2059},
            {"id": 53865410, "public_ipaddr": "198.51.100.8", "gpu_name": "RTX 3060", "num_gpus": 2},
            {"id": 1, "public_ipaddr": "", "dph_total": 0.5},
        ]
    }
    boxes = boxes_from_body(body)
    assert boxes["203.0.113.10"] == {
        "provider": "Vast",
        "box_id": "55406468",
        "label": "GB10 x1",
        "url": VAST_CONSOLE_URL,
        "usd_per_hour": 0.2059,
    }
    # Placed, but no price: unknown, not free.
    assert boxes["198.51.100.8"].label == "RTX 3060 x2"
    assert boxes["198.51.100.8"].usd_per_hour is None
    assert set(boxes) == {"203.0.113.10", "198.51.100.8"}
    assert boxes_from_body({}) == {}


def test_a_runpod_box_is_a_running_pod_with_an_address():
    pods = [
        {
            "id": "tafo5",
            "name": "llmao-qwen38-27b",
            "desiredStatus": "RUNNING",
            "publicIp": "203.0.113.20",
            "costPerHr": 2.49,
        },
        # A stopped pod still reports a price; it is not one of our boxes.
        {"id": "tjfs5", "name": "testing", "desiredStatus": "EXITED", "publicIp": "", "costPerHr": 0.89},
        {"id": "x", "desiredStatus": "RUNNING", "publicIp": "", "costPerHr": 0.89},
    ]
    assert boxes_from_pods(pods) == {
        "203.0.113.20": {
            "provider": "RunPod",
            "box_id": "tafo5",
            "label": "llmao-qwen38-27b",
            "url": "",
            "usd_per_hour": 2.49,
        }
    }
    assert boxes_from_pods({"error": "nope"}) == {}


def test_a_box_lands_on_every_server_on_it():
    a, b, other = _server("203.0.113.10", 8001), _server("203.0.113.10", 8002), _server("198.51.100.8")
    fleet = Fleet(None, [a, b, other])
    fleet.apply_boxes({"203.0.113.10": _box(0.206)})
    assert (a.box_usd_per_hour, b.box_usd_per_hour, other.box_usd_per_hour) == (0.206, 0.206, None)
    assert a.box.label == "GB10 x1"
    assert other.box is None


def _fleet_with_runpod(servers):
    return Fleet(edict(providers=edict(runpod=edict(api_key="rp-test"))), servers)


def _pod(ip, price=2.49):
    return {"id": "p", "name": "n", "desiredStatus": "RUNNING", "publicIp": ip, "costPerHr": price}


def test_the_runpod_listing_is_read_into_the_fleet_and_placed_on_the_next_pass():
    srv = _server("203.0.113.20")
    fleet = _fleet_with_runpod([srv])

    async def read():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[_pod("203.0.113.20")]))
        async with httpx.AsyncClient(transport=transport) as client:
            await fleet.read_runpod_listing(client)

    asyncio.run(read())
    assert srv.box is None, "the runner only reads; the lifecycle places"
    fleet.place_servers()
    assert srv.box_usd_per_hour == 2.49
    assert srv.unlisted is False


def test_without_a_runpod_key_the_runner_ends_at_once():
    fleet = Fleet(edict(), [_server("203.0.113.20")])
    asyncio.run(asyncio.wait_for(fleet.run_runpod_listing(), timeout=1))


def _fleet_renting_from_both(servers):
    cfg = edict(providers=edict(vast=edict(api_key="v"), runpod=edict(api_key="rp")))
    return Fleet(cfg, servers)


def test_a_host_no_provider_lists_is_unlisted_and_said_once(caplog):
    gone, here = _server("203.0.113.30"), _server("203.0.113.20")
    gone.box = _box(0.22)
    fleet = _fleet_renting_from_both([gone, here])
    fleet.vast_boxes = edict()
    fleet.runpod_boxes = edict({"203.0.113.20": _box(2.49, provider="RunPod")})
    with caplog.at_level("INFO", logger="llmao.fleet"):
        for _ in range(UNLISTED_PASSES + 1):
            fleet.place_servers()
    assert (gone.unlisted, here.unlisted) == (True, False)
    # Its old instance and price no longer apply.
    assert gone.box is None
    assert caplog.text.count("no provider lists 203.0.113.30") == 1
    # Listed again: said once, and placed.
    fleet.vast_boxes = edict({"203.0.113.30": _box(0.22)})
    with caplog.at_level("INFO", logger="llmao.fleet"):
        fleet.place_servers()
    assert gone.unlisted is False
    assert "203.0.113.30 is listed again" in caplog.text


def test_a_host_no_configured_provider_could_list_is_not_unlisted():
    """A live box rented from a provider whose key is not configured.

    An unconfigured provider is not a missing list, so `complete` goes true as
    soon as Vast answers and every host of the other provider looks absent.
    A host no listing has ever named is unknown, not gone.
    """
    never = _server("203.0.113.40")
    fleet = Fleet(edict(providers=edict(vast=edict(api_key="v"))), [never])
    fleet.vast_boxes = edict()
    for _ in range(UNLISTED_PASSES + 2):
        fleet.place_servers()
    assert never.unlisted is False


def test_a_provisioning_gap_shorter_than_the_threshold_is_not_a_dead_rental():
    """The production incident: a RunPod pod absent while it provisions.

    A pod was missing from the pod list for over 90 minutes after a
    recreate, then appeared as RUNNING. One absent listing must not conclude
    the rental ended, because the route gets deleted and re-added on
    alternating passes for as long as the gap lasts.
    """
    pod = _server("203.0.113.50")
    fleet = _fleet_renting_from_both([pod])
    listed = edict({"203.0.113.50": _box(2.49, provider="RunPod")})
    fleet.vast_boxes, fleet.runpod_boxes = edict(), listed
    fleet.place_servers()
    assert pod.unlisted is False
    # Absent, but for fewer passes than the threshold.
    fleet.runpod_boxes = edict()
    for _ in range(UNLISTED_PASSES - 1):
        fleet.place_servers()
    assert pod.unlisted is False, "a provisioning gap is not a dead rental"
    # Back in the listing: the count resets, so a flapping host never flips.
    fleet.runpod_boxes = listed
    fleet.place_servers()
    assert pod.unlisted is False
    fleet.runpod_boxes = edict()
    for _ in range(UNLISTED_PASSES - 1):
        fleet.place_servers()
    assert pod.unlisted is False, "the count must reset on a sighting"


def test_nothing_is_concluded_until_every_provider_has_answered():
    srv = _server("203.0.113.30")
    fleet = _fleet_renting_from_both([srv])
    fleet.vast_boxes = edict()  # Vast answered; RunPod not yet
    fleet.place_servers()
    assert srv.unlisted is False
    # A fleet that rents from nobody (a local one) has nothing unlisted.
    local = Fleet(edict(), [_server("127.0.0.1")])
    local.place_servers()
    assert local.servers[0].unlisted is False


def test_each_row_carries_its_servers_share_of_the_box():
    """Two servers on a $0.206 box are $0.103 each, so adding rows up gives the bill."""
    a = _server("203.0.113.10", 8001, public_port=8001)
    b = _server("203.0.113.10", 8002, public_port=8002)
    unpriced = _server("198.51.100.8", public_port=8001)
    for srv in (a, b):
        srv.box = _box(0.206)
    rollup = HourlyRollup(15)
    sampler = MetricsSampler(Fleet(None, [a, b, unpriced]), rollup, interval_s=15)

    async def one_pass():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, text=""))
        async with httpx.AsyncClient(transport=transport) as client:
            await sampler.sample_once(client)

    asyncio.run(one_pass())
    hours = {h.server: h.row(15).usd_per_hour for h in rollup.take_finished(time.time() + HOUR_S)}
    assert hours == {"203.0.113.10:8001": 0.103, "203.0.113.10:8002": 0.103, "198.51.100.8:8001": None}

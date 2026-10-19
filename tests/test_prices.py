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
"""What each box rents for: Vast and RunPod prices, onto the capacity rows."""

from __future__ import annotations

import asyncio
import time

import httpx
from easydict import EasyDict as edict  # noqa: N813

from llmao.capacity import HOUR_S, HourlyRollup
from llmao.fleet import Fleet, VllmServer
from llmao.runpod_client import price_map_from_pods
from llmao.sampler import MetricsSampler
from llmao.vast_client import price_map_from_body


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


def test_vast_prices_are_dph_total_by_public_ip():
    body = {
        "instances": [
            {"public_ipaddr": "203.0.113.10", "dph_total": 0.2059},
            {"public_ipaddr": "198.51.100.8", "dph_total": "1.1867"},
            {"public_ipaddr": "192.0.2.4"},  # no price: unknown, not free
            {"public_ipaddr": "", "dph_total": 0.5},
        ]
    }
    assert price_map_from_body(body) == {"203.0.113.10": 0.2059, "198.51.100.8": 1.1867}
    assert price_map_from_body({}) == {}


def test_runpod_prices_count_only_running_pods_with_an_address():
    pods = [
        {"desiredStatus": "RUNNING", "publicIp": "203.0.113.20", "costPerHr": 2.49},
        # A stopped pod still reports a price; it is not one of our boxes.
        {"desiredStatus": "EXITED", "publicIp": "", "costPerHr": 0.89},
        {"desiredStatus": "RUNNING", "publicIp": "", "costPerHr": 0.89},
    ]
    assert price_map_from_pods(pods) == {"203.0.113.20": 2.49}
    assert price_map_from_pods({"error": "nope"}) == {}


def test_a_price_lands_on_every_server_of_its_box():
    a, b, other = _server("203.0.113.10", 8001), _server("203.0.113.10", 8002), _server("198.51.100.8")
    fleet = Fleet(None, [a, b, other])
    fleet.apply_prices({"203.0.113.10": 0.206})
    assert (a.box_usd_per_hour, b.box_usd_per_hour, other.box_usd_per_hour) == (0.206, 0.206, None)


def _fleet_with_runpod(servers):
    return Fleet(edict(providers=edict(runpod=edict(api_key="rp-test"))), servers)


def test_runpod_is_asked_only_while_a_server_is_unpriced():
    srv = _server("203.0.113.20")
    fleet = _fleet_with_runpod([srv])
    asked = []

    def answer(request):
        asked.append(request.url.path)
        return httpx.Response(200, json=[{"desiredStatus": "RUNNING", "publicIp": "203.0.113.20", "costPerHr": 2.49}])

    async def two_passes():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            await fleet.refresh_runpod_prices(client)
            await fleet.refresh_runpod_prices(client)

    asyncio.run(two_passes())
    assert srv.box_usd_per_hour == 2.49
    assert asked == ["/v1/pods"]


def test_without_a_runpod_key_runpod_is_never_asked():
    fleet = Fleet(edict(), [_server("203.0.113.20")])

    async def one_pass():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: 1 / 0)) as client:
            await fleet.refresh_runpod_prices(client)

    asyncio.run(one_pass())


def test_each_row_carries_its_servers_share_of_the_box():
    """Two servers on a $0.206 box are $0.103 each, so adding rows up gives the bill."""
    a = _server("203.0.113.10", 8001, public_port=8001)
    b = _server("203.0.113.10", 8002, public_port=8002)
    unpriced = _server("198.51.100.8", public_port=8001)
    for srv in (a, b):
        srv.box_usd_per_hour = 0.206
    rollup = HourlyRollup(15)
    sampler = MetricsSampler(Fleet(None, [a, b, unpriced]), rollup, interval_s=15)

    async def one_pass():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, text=""))
        async with httpx.AsyncClient(transport=transport) as client:
            await sampler.sample_once(client)

    asyncio.run(one_pass())
    hours = {h.server: h.row(15).usd_per_hour for h in rollup.take_finished(time.time() + HOUR_S)}
    assert hours == {"203.0.113.10:8001": 0.103, "203.0.113.10:8002": 0.103, "198.51.100.8:8001": None}

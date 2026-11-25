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

"""fleet.servers_hook: the script runs when a server's address changes, once per change, and is retried on failure."""

import asyncio

import pytest
from box import Box

from llmao.fleet import HOOK_RETRY_S, Fleet, validate_fleet
from llmao.models import load_models
from llmao.rental import ProviderRentals, Rental
from tests.test_fleet import EXAMPLE, _cfg

HOOK = {"script": "bin/sync-nodeping.py", "settings": {"api_key": "np-key", "prefix": "rai"}}


class _Runner:
    """Stands in for run_hook: records each document and answers as told."""

    def __init__(self, code=0, output="nodeping: ok"):
        self.code = code
        self.output = output
        self.runs = []

    async def __call__(self, script, document):
        self.runs.append((script, document))
        return self.code, self.output


def _fleet(hook=HOOK):
    cfg = _cfg({"58458812": [["qwen3-8b", 8001]]})
    if hook is not None:
        cfg.fleet.servers_hook = Box(hook)
    models = load_models(EXAMPLE)
    validate_fleet(cfg, models=models)
    return Fleet.from_cfg(cfg, models=models)


def _place(fleet, address, public_port):
    rental = Rental(
        provider="Vast",
        id="58458812",
        label="x",
        url="",
        usd_per_hour=None,
        host=address,
        name="58458812",
        status="running",
        ports=Box({"8001": public_port}),
    )
    fleet.apply_rentals(ProviderRentals("Vast", {rental.id: rental}))


async def _run(fleet, **kw):
    fleet.run_servers_hook(**kw)
    if fleet.hook_task is not None:
        await fleet.hook_task


def test_no_hook_configured_runs_nothing():
    fleet = _fleet(hook=None)
    fleet.hook_runner = _Runner()
    asyncio.run(_run(fleet))
    assert fleet.hook_task is None


def test_the_hook_runs_once_per_change_with_the_settings_and_every_server():
    fleet = _fleet()
    runner = _Runner()
    fleet.hook_runner = runner

    async def scenario():
        # The first pass: the server is known but unplaced, and that is sent too.
        await _run(fleet)
        await _run(fleet)
        assert len(runner.runs) == 1
        script, document = runner.runs[0]
        assert script.name == "sync-nodeping.py" and script.parent.name == "bin"
        assert document.settings == {"api_key": "np-key", "prefix": "rai"}
        assert document.servers == [
            {"model": "qwen3-8b", "host": "58458812", "listen_port": 8001, "provider": None, "health_url": None}
        ]
        # The listing places it: one more run, with the address.
        _place(fleet, "203.0.113.5", 18490)
        await _run(fleet)
        await _run(fleet)
        assert len(runner.runs) == 2
        server = runner.runs[1][1].servers[0]
        assert server.provider == "Vast"
        assert server.health_url == "http://203.0.113.5:18490/health"
        # The machine moves: again.
        _place(fleet, "203.0.113.6", 18490)
        await _run(fleet)
        assert len(runner.runs) == 3
        assert runner.runs[2][1].servers[0].health_url == "http://203.0.113.6:18490/health"

    asyncio.run(scenario())


def test_a_failed_run_is_retried_after_the_retry_interval_or_at_once_on_a_change():
    fleet = _fleet()
    runner = _Runner(code=1, output="sync-nodeping: GET /checks: HTTP 503")
    fleet.hook_runner = runner

    async def scenario():
        await _run(fleet, now=1000.0)
        assert len(runner.runs) == 1
        assert fleet.hook_sent is None
        # The same servers, too soon: not again.
        await _run(fleet, now=1000.0 + HOOK_RETRY_S - 1)
        assert len(runner.runs) == 1
        # Past the interval: again.
        await _run(fleet, now=1000.0 + HOOK_RETRY_S + 1)
        assert len(runner.runs) == 2
        # Different servers, right away: again, and once it succeeds the servers are what was sent.
        runner.code = 0
        _place(fleet, "203.0.113.5", 18490)
        await _run(fleet, now=1000.0 + HOOK_RETRY_S + 2)
        assert len(runner.runs) == 3
        assert fleet.hook_sent == fleet.servers_payload()
        assert fleet.hook_failed is None

    asyncio.run(scenario())


def test_a_hook_whose_script_is_missing_fails_the_fleet_build():
    cfg = _cfg({"58458812": [["qwen3-8b", 8001]]})
    cfg.fleet.servers_hook = Box({"script": "bin/no-such-script.py"})
    with pytest.raises(ValueError, match=r"servers_hook\.script"):
        validate_fleet(cfg, models=load_models(EXAMPLE))
    cfg.fleet.servers_hook = Box({"script": "bin/sync-nodeping.py", "settings": "rai"})
    with pytest.raises(ValueError, match="settings must be a mapping"):
        validate_fleet(cfg, models=load_models(EXAMPLE))

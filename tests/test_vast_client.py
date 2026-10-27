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

"""Vast show-instances → listen/public port map (no live API)."""

import asyncio
import logging

import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.fleet import Fleet, VllmServer, validate_fleet
from llmao.model_status import AWAITING
from llmao.models import load_models
from llmao.vast_client import port_map_from_body, ports_from_instance
from tests.test_fleet import EXAMPLE, FLEET_KNOBS

INSTANCE = {
    "id": 55406468,
    "public_ipaddr": "203.0.113.10",
    "ports": {
        "8001/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41234"}],
        "22/tcp": [{"HostIp": "0.0.0.0", "HostPort": "20000"}],
    },
}


def test_ports_from_instance():
    ports = ports_from_instance(INSTANCE)
    assert ports["8001"] == 41234
    assert ports["22"] == 20000


def test_port_map_from_body():
    mapping = port_map_from_body(
        {
            "instances": [INSTANCE],
            "total_instances": 1,
        }
    )
    assert mapping["203.0.113.10"]["8001"] == 41234


def test_second_row_for_the_same_ip_keeps_both_ports():
    other = {
        "public_ipaddr": "203.0.113.10",
        "ports": {"8002/tcp": [{"HostIp": "0.0.0.0", "HostPort": "41235"}]},
    }
    mapping = port_map_from_body({"instances": [INSTANCE, other], "total_instances": 2})
    assert mapping["203.0.113.10"]["8001"] == 41234
    assert mapping["203.0.113.10"]["8002"] == 41235


def test_skip_no_ip_or_ports():
    mapping = port_map_from_body(
        {
            "instances": [
                {"public_ipaddr": "", "ports": INSTANCE["ports"]},
                {"public_ipaddr": "203.0.113.11"},
            ],
            "total_instances": 2,
        }
    )
    assert list(mapping.keys()) == []


def test_truncated_page_still_returns_what_we_have(caplog):
    mapping = port_map_from_body(
        {
            "instances": [INSTANCE],
            "total_instances": 40,
        }
    )
    assert mapping["203.0.113.10"]["8001"] == 41234
    assert "not paginating" in caplog.text


def test_apply_port_map():
    s = VllmServer(
        model_name="gemma4-26b",
        name="gemma4-26b",
        host="203.0.113.10",
        listen_port=8001,
        hf_model="google/gemma",
        api_key="sk-x",
        args=[],
    )
    assert s.public_port is None
    fleet = Fleet(cfg=None, servers=[s])
    fleet.apply_port_map(port_map_from_body({"instances": [INSTANCE], "total_instances": 1}))
    assert s.public_port == 41234
    assert s.listen_port == 8001
    assert s.api_base == "http://203.0.113.10:41234/v1"


def test_apply_port_map_missing_host_stays_awaiting():
    s = VllmServer(
        model_name="gemma4-26b",
        name="gemma4-26b",
        host="198.51.100.1",
        listen_port=8001,
        hf_model="google/gemma",
        api_key="sk-x",
        args=[],
    )
    fleet = Fleet(cfg=None, servers=[s])
    fleet.apply_port_map(port_map_from_body({"instances": [INSTANCE], "total_instances": 1}))
    assert s.public_port is None
    assert s.state == AWAITING


def _vast_cfg(*, providers=None, fleet_vast=None, host="127.0.0.1"):
    fleet = {"hosts": {host: [["gemma4-26b", 8001]]}, "vllm_api_salt": "test-vllm-salt", **FLEET_KNOBS}
    if fleet_vast is not None:
        fleet["vast"] = fleet_vast
    cfg = {"fleet": fleet, "models_path": str(EXAMPLE)}
    if providers is not None:
        cfg["providers"] = providers
    return edict(cfg)


def test_the_vast_key_lives_under_providers():
    cfg = _vast_cfg(providers={"vast": {"api_key": "abc"}})
    validate_fleet(cfg, models=load_models(EXAMPLE))
    fleet = Fleet.from_cfg(cfg, models=load_models(EXAMPLE))
    # With a Vast key the public port is Vast's to say, not the listen port.
    assert fleet.servers[0].public_port is None
    assert fleet.deployments[0].provider is None


def test_a_gpu_host_takes_only_its_key():
    cfg = _vast_cfg(providers={"runpod": {"api_key": "abc", "models": {"gemma4-26b": "x"}}})
    with pytest.raises(ValueError, match=r"providers\.runpod is a GPU host and takes only api_key, not models"):
        validate_fleet(cfg, models=load_models(EXAMPLE))


def test_a_gpu_host_needs_its_key():
    cfg = _vast_cfg(providers={"runpod": {}})
    with pytest.raises(ValueError, match=r"providers\.runpod\.api_key is required"):
        validate_fleet(cfg, models=load_models(EXAMPLE))


def test_the_old_place_still_works_with_a_warning(caplog):
    cfg = _vast_cfg(fleet_vast={"api_key": "abc"})
    with caplog.at_level(logging.WARNING, logger="llmao.intent"):
        validate_fleet(cfg, models=load_models(EXAMPLE))
    assert "fleet.vast.api_key is deprecated; move it to providers.vast.api_key" in caplog.text
    assert Fleet.from_cfg(cfg, models=load_models(EXAMPLE)).servers[0].public_port is None


def test_the_key_in_both_places_is_refused():
    cfg = _vast_cfg(providers={"vast": {"api_key": "abc"}}, fleet_vast={"api_key": "abc"})
    with pytest.raises(ValueError, match="keep one"):
        validate_fleet(cfg, models=load_models(EXAMPLE))


def test_validate_fleet_requires_vast_api_key():
    cfg = edict(
        {
            "fleet": {"hosts": {"127.0.0.1": [["gemma4-26b", 8001]]}, "vast": {}, **FLEET_KNOBS},
            "models_path": str(EXAMPLE),
        }
    )
    with pytest.raises(ValueError, match=r"fleet\.vast\.api_key"):
        validate_fleet(cfg, models=load_models(EXAMPLE))


def test_validate_fleet_vast_ok():
    cfg = edict(
        {
            "fleet": {
                "hosts": {"127.0.0.1": [["gemma4-26b", 8001]]},
                "vllm_api_salt": "test-vllm-salt",
                "vast": {"api_key": "abc"},
                **FLEET_KNOBS,
            },
            "models_path": str(EXAMPLE),
        }
    )
    validate_fleet(cfg, models=load_models(EXAMPLE))
    fleet = Fleet.from_cfg(cfg, models=load_models(EXAMPLE))
    assert fleet.servers[0].public_port is None


def test_refresh_uses_dotted_api_key():
    cfg = edict(
        {
            "fleet": {
                "hosts": {"203.0.113.10": [["gemma4-26b", 8001]]},
                "vllm_api_salt": "test-vllm-salt",
                **FLEET_KNOBS,
            },
            "providers": {"vast": {"api_key": "secret"}},
            "models_path": str(EXAMPLE),
        }
    )
    fleet = Fleet.from_cfg(cfg, models=load_models(EXAMPLE))

    class _Client:
        async def get(self, url, **kwargs):
            assert kwargs["headers"]["Authorization"] == "Bearer secret"

            class _Resp:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"instances": [INSTANCE], "total_instances": 1}

            return _Resp()

    asyncio.run(fleet.refresh_public_ports(_Client()))
    # The refresh reads; the lifecycle's placement stamps the port from the box.
    assert fleet.servers[0].public_port is None
    fleet.place_servers()
    assert fleet.servers[0].public_port == 41234

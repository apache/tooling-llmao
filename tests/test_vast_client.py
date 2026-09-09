"""Vast show-instances → listen/public port map (no live API)."""
import asyncio

import pytest
from easydict import EasyDict as edict

from llmao.fleet import Fleet, Server, validate_fleet
from llmao.models import load_model_list
from llmao.vast_client import port_map_from_body, ports_from_instance
from tests.test_fleet import EXAMPLE, FLEET_KNOBS

INSTANCE = {
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
    mapping = port_map_from_body({
        "instances": [INSTANCE],
        "total_instances": 1,
    })
    assert mapping["203.0.113.10"]["8001"] == 41234


def test_skip_no_ip_or_ports():
    mapping = port_map_from_body({
        "instances": [
            {"public_ipaddr": "", "ports": INSTANCE["ports"]},
            {"public_ipaddr": "203.0.113.11"},
        ],
        "total_instances": 2,
    })
    assert list(mapping.keys()) == []


def test_truncated_page_still_returns_what_we_have(caplog):
    mapping = port_map_from_body({
        "instances": [INSTANCE],
        "total_instances": 40,
    })
    assert mapping["203.0.113.10"]["8001"] == 41234
    assert "not paginating" in caplog.text


def test_apply_port_map():
    s = Server(
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
    assert s.box_json()["port"] == 8001
    assert s.api_base == "http://203.0.113.10:41234"


def test_apply_port_map_missing_host_stays_pending():
    s = Server(
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
    assert s.state == Server.PENDING


def test_validate_fleet_requires_vast_api_key():
    cfg = edict({
        "fleet": {"hosts": {"127.0.0.1": [["gemma4-26b", 8001]]}, "vast": {}, **FLEET_KNOBS},
        "models_path": str(EXAMPLE),
    })
    with pytest.raises(ValueError, match="fleet.vast.api_key"):
        validate_fleet(cfg, models=load_model_list(EXAMPLE))


def test_validate_fleet_vast_ok():
    cfg = edict({
        "fleet": {
            "hosts": {"127.0.0.1": [["gemma4-26b", 8001]]},
            "vast": {"api_key": "abc"},
            **FLEET_KNOBS,
        },
        "models_path": str(EXAMPLE),
    })
    validate_fleet(cfg, models=load_model_list(EXAMPLE))
    fleet = Fleet.from_cfg(cfg, models=load_model_list(EXAMPLE))
    assert fleet.servers[0].public_port is None


def test_refresh_uses_dotted_api_key():
    cfg = edict({
        "fleet": {
            "hosts": {"203.0.113.10": [["gemma4-26b", 8001]]},
            "vast": {"api_key": "secret"},
            **FLEET_KNOBS,
        },
        "models_path": str(EXAMPLE),
    })
    fleet = Fleet.from_cfg(cfg, models=load_model_list(EXAMPLE))

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
    assert fleet.servers[0].public_port == 41234

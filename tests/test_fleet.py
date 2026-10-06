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

"""config_for_host: JSON for a client IP from fleet.hosts + models.yaml."""

import asyncio
from pathlib import Path

import httpx
import pytest
import yaml
from easydict import EasyDict as edict  # noqa: N813

from llmao.fleet import (
    HOST_HEADER,
    Fleet,
    UnknownHostError,
    client_ip,
    config_for_host,
    normalize_peer_ip,
    parse_host_row,
    snapshot_for_status,
    validate_fleet,
)
from llmao.litellm_client import _norm_base
from llmao.model_status import LOADING, deployment_status
from llmao.models import load_models
from llmao.vllm_api_key import derive_vllm_api_key

EXAMPLE = Path(__file__).resolve().parent.parent / "models.yaml"
EXAMPLE_CFG = Path(__file__).resolve().parent.parent / "config.yaml.example"

FLEET_KNOBS = {
    "health_interval_s": 45,
    "health_timeout_s": 3,
    "health_grace_s": 1800,
    "health_fail_threshold": 3,
    "skew_interval_s": 180,
    "litellm_health_interval_s": 14400,
}


def _cfg(hosts, models_path=EXAMPLE):
    return edict(
        {
            "fleet": {"hosts": hosts, "vllm_api_salt": "test-vllm-salt", **FLEET_KNOBS},
            "models_path": str(models_path),
        }
    )


def test_example_primary_host():
    cfg = edict(yaml.safe_load(EXAMPLE_CFG.read_text(encoding="utf-8")))
    cfg.fleet.vllm_api_salt = "test-vllm-salt"
    models = load_models(EXAMPLE)
    validate_fleet(cfg, models=models)
    payload = config_for_host("127.0.0.1", models=models, cfg=cfg)
    assert payload["host"] == "127.0.0.1"
    assert "set_id" not in payload
    names = [s["name"] for s in payload["servers"]]
    assert "gemma4-26b" in names
    assert "qwen3-8b" in names
    gemma = next(s for s in payload["servers"] if s["name"] == "gemma4-26b")
    assert gemma["model"] == "google/gemma-4-26B-A4B-it"
    assert gemma["host"] == "127.0.0.1"
    assert gemma["port"] == 8001


def test_unknown_host():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    validate_fleet(cfg, models=models)
    with pytest.raises(UnknownHostError):
        config_for_host("10.0.0.9", models=models, cfg=cfg)


def _approvable(cfg, models):
    fleet = Fleet(cfg=cfg, servers=[], models={m.model_name: m for m in models})
    return fleet


def test_approved_unknown_host_is_served():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    fleet.approve_models("198.51.100.8", ["qwen3-8b", "gemma4-26b"])
    assert fleet.admin_label("198.51.100.8").label == "approved"
    payload = config_for_host(
        "198.51.100.8",
        models=models,
        cfg=cfg,
        approved=fleet.approved_models["198.51.100.8"],
    )
    assert [s["name"] for s in payload["servers"]] == ["qwen3-8b", "gemma4-26b"]
    assert [s["port"] for s in payload["servers"]] == [8001, 8002]
    assert payload["servers"][0]["model"] == "Qwen/Qwen3-8B-FP8"


def test_approve_rejects_bad_selections():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    fleet.models["claude"] = edict(model_name="claude", model_info=edict(self_hosted=False))
    for names in ([], ["nope"], ["claude"]):
        with pytest.raises(ValueError):
            fleet.approve_models("198.51.100.8", names)
    assert fleet.approved_models == {}
    assert fleet.admin_labels == {}
    with pytest.raises(ValueError, match="already in fleet"):
        fleet.approve_models("127.0.0.1", ["gemma4-26b"])


def test_config_fetch_materializes_an_approved_host():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    fleet.approve_models("198.51.100.8", ["qwen3-8b", "gemma4-26b"])
    fleet.note_config_fetch("198.51.100.8", now=50.0)
    fleet.note_config_fetch("198.51.100.8", now=60.0)
    assert [s.name for s in fleet.servers] == ["qwen3-8b", "gemma4-26b"]
    assert [s.listen_port for s in fleet.servers] == [8001, 8002]
    assert len(fleet.deployments) == 2
    assert all(not d.in_litellm for d in fleet.deployments)
    assert all(s.config_served_at == 60.0 for s in fleet.servers)
    view = deployment_status(snapshot_for_status(fleet.deployments[0], cfg, 60.0), cfg.fleet, 60.0)
    assert view.lifecycle == LOADING
    # A host config.yaml already names is not built from an approval.
    fleet.approved_models["127.0.0.1"] = ["qwen3-8b"]
    fleet.note_config_fetch("127.0.0.1", now=70.0)
    assert [s.host for s in fleet.servers] == ["198.51.100.8", "198.51.100.8"]


def test_probe_moves_the_materialized_server_to_healthy():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    fleet.approve_models("198.51.100.8", ["qwen3-8b"])
    fleet.note_config_fetch("198.51.100.8", now=50.0)
    srv = fleet.servers[0]
    srv.public_port = 18001
    srv.record_probe(True, now=55.0, grace_s=1800, fail_threshold=3, reached=True)
    assert srv.state == "healthy"
    assert fleet.deployments[0].api_base == "http://198.51.100.8:18001/v1"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_vast_refused_port_stays_unknown():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    fleet.approve_models("198.51.100.8", ["qwen3-8b"])

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def run():
        async with _client(handler) as client:
            await fleet.adopt_vast_instances({"198.51.100.8": {"8001": 18001}}, client, now=10.0)

    asyncio.run(run())
    assert fleet.servers == []


def test_vast_503_with_approval_is_loading():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    fleet.approve_models("198.51.100.8", ["qwen3-8b"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def run():
        async with _client(handler) as client:
            await fleet.adopt_vast_instances({"198.51.100.8": {"8001": 18001}}, client, now=10.0)

    asyncio.run(run())
    assert len(fleet.servers) == 1
    assert fleet.servers[0].public_port == 18001
    assert fleet.servers[0].state == LOADING
    assert not fleet.deployments[0].in_litellm


def test_vast_200_rebuilds_without_an_approval():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3-8B-FP8"}]})
        return httpx.Response(200)

    async def run():
        async with _client(handler) as client:
            await fleet.adopt_vast_instances({"198.51.100.8": {"8001": 18001}}, client, now=10.0)

    asyncio.run(run())
    assert [s.model_name for s in fleet.servers] == ["qwen3-8b"]
    assert fleet.servers[0].state == "healthy"
    assert fleet.deployments[0].api_base == "http://198.51.100.8:18001/v1"


def test_approval_drops_when_the_host_joins_config():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    fleet.approve_models("198.51.100.8", ["qwen3-8b"])
    cfg.fleet.hosts["198.51.100.8"] = [["qwen3-8b", 8001]]
    fleet.note_unknown_config_fetch("203.0.113.9", now=40)
    assert "198.51.100.8" not in fleet.approved_models


def test_optional_name_two_copies():
    cfg = _cfg(
        {
            "10.0.0.1": [
                ["qwen3-8b", 8003],
                ["qwen3-8b", 8004, "qwen3-8b-b"],
            ]
        }
    )
    models = load_models(EXAMPLE)
    validate_fleet(cfg, models=models)
    payload = config_for_host("10.0.0.1", models=models, cfg=cfg)
    assert [s["name"] for s in payload["servers"]] == ["qwen3-8b", "qwen3-8b-b"]
    assert [s["port"] for s in payload["servers"]] == [8003, 8004]


def test_duplicate_name():
    cfg = _cfg(
        {
            "10.0.0.1": [
                ["qwen3-8b", 8003],
                ["qwen3-8b", 8004],
            ]
        }
    )
    with pytest.raises(ValueError, match="duplicate name"):
        validate_fleet(cfg, models=load_models(EXAMPLE))


def test_duplicate_port():
    cfg = _cfg(
        {
            "10.0.0.1": [
                ["gemma4-26b", 8001],
                ["qwen3-8b", 8001],
            ]
        }
    )
    with pytest.raises(ValueError, match="duplicate port"):
        validate_fleet(cfg, models=load_models(EXAMPLE))


def test_unknown_model():
    cfg = _cfg({"10.0.0.1": [["nope", 9]]})
    with pytest.raises(ValueError, match="unknown model"):
        validate_fleet(cfg, models=load_models(EXAMPLE))


def test_normalize_peer_ip():
    assert normalize_peer_ip("::ffff:203.0.113.10") == "203.0.113.10"
    assert normalize_peer_ip("203.0.113.10") == "203.0.113.10"


def test_client_ip_xff_leftmost():
    assert client_ip(remote_addr="10.0.0.1", forwarded_for="203.0.113.10, 10.0.0.1") == "203.0.113.10"


def test_client_ip_no_xff():
    assert client_ip(remote_addr="203.0.113.10", forwarded_for=None) == "203.0.113.10"
    assert client_ip(remote_addr="::ffff:203.0.113.10", forwarded_for="") == "203.0.113.10"


def test_client_ip_claimed_host_not_peer():
    """Vast transparent proxy: TCP peer is not the instance. Header is the key."""
    assert (
        client_ip(
            remote_addr="10.0.0.9",
            forwarded_for=None,
            claimed_host="203.0.113.10",
        )
        == "203.0.113.10"
    )
    assert (
        client_ip(
            remote_addr="10.0.0.9",
            forwarded_for="198.51.100.1",
            claimed_host="203.0.113.10",
        )
        == "203.0.113.10"
    )
    assert HOST_HEADER == "X-LLMAO-Host"


def test_norm_base_strips_trailing_v1():
    """Why the normalisation exists.

    An older LiteLLM appended /v1/chat/completions to api_base, so a base
    that already ended in /v1 resolved to /v1/v1/... and 404'd at the origin.
    LiteLLM then reported an OpenAI authentication error naming
    platform.openai.com -- so the symptom pointed at the caller's key rather
    than at the route. This happened in production against the Fastly-fronted
    Gemma endpoint. The current LiteLLM appends only /chat/completions, so a
    /v1-suffixed base is what vLLM needs now; routes pushed under the old
    behaviour may lack it, and the comparison must match across both forms.
    """
    assert _norm_base("https://llm.tooling.apache.org/v1") == "https://llm.tooling.apache.org"
    assert _norm_base("https://llm.tooling.apache.org/v1/") == "https://llm.tooling.apache.org"
    assert _norm_base("http://127.0.0.1:8003/v1") == "http://127.0.0.1:8003"


def test_norm_base_leaves_other_paths_alone():
    """Only a trailing /v1 is stripped -- not a path that merely contains it."""
    assert _norm_base("http://127.0.0.1:8003") == "http://127.0.0.1:8003"
    assert _norm_base("http://127.0.0.1:8003/") == "http://127.0.0.1:8003"
    assert _norm_base("https://example.com/v1/proxy") == "https://example.com/v1/proxy"
    assert _norm_base("") == ""
    assert _norm_base(None) == ""


def test_norm_base_makes_skew_comparison_match():
    """The comparison this exists for.

    check_config_skew compares VllmServer.api_base against what LiteLLM reports.
    VllmServer.api_base ends in /v1; a deployment pushed before that, or a
    models.yaml row, may lack it. Without normalisation on both sides the
    same deployment never matches, and the skew check reports a missing
    deployment that is actually present.
    """
    assert _norm_base("http://100.105.28.100:8003/v1") == _norm_base("http://100.105.28.100:8003")


def test_from_row_api_key_is_salt_hmac():
    """Box JSON carries the salt HMAC, not the legacy shared bearer."""
    cfg = _cfg({"10.0.0.1": [["qwen3-8b", 8003]]})
    cfg.fleet.selfhost_api_key = "sk-fleet"
    models = load_models(EXAMPLE)  # definitions carry no api_key
    payload = config_for_host("10.0.0.1", models=models, cfg=cfg)
    assert payload["servers"][0]["api_key"] == derive_vllm_api_key("test-vllm-salt", "10.0.0.1", 8003)
    assert payload["servers"][0]["api_key"] != "sk-fleet"


def test_from_row_requires_salt():
    """A fleet config that never defines the salt fails at attribute access."""
    cfg = edict(
        {
            "fleet": {"hosts": {"10.0.0.1": [["qwen3-8b", 8003]]}, **FLEET_KNOBS},
            "models_path": str(EXAMPLE),
        }
    )
    with pytest.raises(AttributeError):
        config_for_host("10.0.0.1", models=load_models(EXAMPLE), cfg=cfg)


def test_host_row_pins_public_port():
    """[model, port, name, public_port] survives a provider lookup.

    Vast exposes its container->public mapping through an API; RunPod does
    not. Without a way to state the public port in config, a RunPod box gets
    no api_base, never goes healthy, and shows unavailable with nothing in
    the UI to say why.
    """
    assert parse_host_row(["qwen3-8b", 8003], "h", 0) == ("qwen3-8b", 8003, "qwen3-8b", None)
    assert parse_host_row(["qwen3-8b", 8003, "b"], "h", 0) == ("qwen3-8b", 8003, "b", None)
    assert parse_host_row(["qwen3-8b", 8003, "b", 15601], "h", 0) == (
        "qwen3-8b",
        8003,
        "b",
        15601,
    )


def test_host_row_null_name_reaches_the_fourth_slot():
    """null for name is how you pin a port without renaming the server."""
    _, _, name, public = parse_host_row(["qwen3-8b", 8003, None, 15601], "h", 0)
    assert name == "qwen3-8b"  # falls back to the model name
    assert public == 15601


def test_host_row_rejects_bad_public_port():
    with pytest.raises(ValueError, match="public_port must be an int"):
        parse_host_row(["qwen3-8b", 8003, None, "nope"], "h", 0)
    with pytest.raises(ValueError, match="must be"):
        parse_host_row(["qwen3-8b", 8003, "b", 1, 2], "h", 0)


def test_pinned_port_survives_apply_port_map():
    """A provider map must not overwrite what config stated.

    apply_port_map runs on every refresh. Without the guard it would log
    "no instance for fleet host" against a RunPod box on every cycle, and a
    future resolver could replace a correct pinned value with a guess.
    """
    cfg = _cfg({"1.2.3.4": [["qwen3-8b", 8003, None, 15601]]})
    fleet = Fleet.from_cfg(cfg, models=load_models(EXAMPLE))
    srv = fleet.servers[0]
    assert srv.public_port == 15601
    assert srv.public_port_pinned is True
    assert srv.api_base == "http://1.2.3.4:15601/v1"

    # A map that knows nothing about this host, then one that disagrees.
    fleet.apply_port_map({})
    assert srv.public_port == 15601
    fleet.apply_port_map({"1.2.3.4": {"8003": 99999}})
    assert srv.public_port == 15601


def test_unpinned_port_still_follows_the_provider_map():
    cfg = _cfg({"1.2.3.4": [["qwen3-8b", 8003]]})
    fleet = Fleet.from_cfg(cfg, models=load_models(EXAMPLE))
    srv = fleet.servers[0]
    assert srv.public_port_pinned is False
    fleet.apply_port_map({"1.2.3.4": {"8003": 12711}})
    assert srv.public_port == 12711

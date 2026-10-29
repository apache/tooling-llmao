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
    client_ip,
    normalize_peer_ip,
    snapshot_for_status,
    validate_fleet,
)
from llmao.intent import Approval, UnknownHostError, derive_vllm_api_key, vllm_api_key
from llmao.model_status import AWAITING, HEALTHY, LOADING, deployment_status
from llmao.models import load_models, vllm_recipe
from llmao.provisioning import config_for_host

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


def test_a_runpod_host_is_keyed_by_pod_id():
    """A RunPod box names itself by pod id, so fleet.hosts holds one as a key."""
    cfg = _cfg({"podid0example": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    validate_fleet(cfg, models=models)
    payload = config_for_host("podid0example", models=models, cfg=cfg)
    assert payload["host"] == "podid0example"
    assert [s["port"] for s in payload["servers"]] == [8001]


def _approvable(cfg, models):
    fleet = Fleet(cfg=cfg, servers=[], models={m.model_name: m for m in models})
    return fleet


def test_approved_unknown_host_is_served():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    approval = fleet.approve_models("198.51.100.8", ["qwen3-8b", "gemma4-26b"], now=100.0)
    assert approval.models == ("qwen3-8b", "gemma4-26b")
    assert fleet.approvals["198.51.100.8"] == approval
    payload = config_for_host(
        "198.51.100.8",
        models=models,
        cfg=cfg,
        approved=list(approval.models),
    )
    assert [s["name"] for s in payload["servers"]] == ["qwen3-8b", "gemma4-26b"]
    assert [s["port"] for s in payload["servers"]] == [8001, 8002]
    assert payload["servers"][0]["model"] == "Qwen/Qwen3-8B-FP8"


def test_approve_rejects_bad_selections():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    fleet.models["claude"] = edict(
        model_name="claude", litellm_params=edict(api_base="https://api.example.com"), model_info=edict()
    )
    for names in ([], ["nope"], ["claude"]):
        with pytest.raises(ValueError):
            fleet.approve_models("198.51.100.8", names)
    assert fleet.approvals == {}
    assert fleet.servers == []
    with pytest.raises(ValueError, match="already in fleet"):
        fleet.approve_models("127.0.0.1", ["gemma4-26b"])


def test_an_approval_leaves_the_strip_and_joins_the_deployments_at_once():
    """Approved is intent: the host stops being unknown and its servers read Awaiting with the rest."""
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    fleet.note_unknown_config_fetch("198.51.100.8", now=40.0)
    assert "198.51.100.8" in fleet.provisioning.unknown
    fleet.approve_models("198.51.100.8", ["qwen3-8b", "gemma4-26b"], now=45.0)
    assert "198.51.100.8" not in fleet.provisioning.unknown
    assert [(s.name, s.listen_port, s.state) for s in fleet.servers] == [
        ("qwen3-8b", 8001, AWAITING),
        ("gemma4-26b", 8002, AWAITING),
    ]
    assert len(fleet.deployments) == 2
    assert all(not d.in_litellm for d in fleet.deployments)
    assert fleet.approval_display("::ffff:198.51.100.8").startswith("Approved · ")
    assert fleet.approval_display("127.0.0.1") == ""
    # A second approval replaces the first; the servers it already has stay.
    fleet.approve_models("198.51.100.8", ["qwen3-8b"], now=46.0)
    assert fleet.approvals["198.51.100.8"].models == ("qwen3-8b",)
    assert len(fleet.servers) == 2


def test_config_fetch_starts_loading_on_the_approved_host():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    fleet.approve_models("198.51.100.8", ["qwen3-8b", "gemma4-26b"])
    fleet.note_config_fetch("198.51.100.8", now=50.0)
    fleet.note_config_fetch("198.51.100.8", now=60.0)
    assert [s.listen_port for s in fleet.servers] == [8001, 8002]
    assert all(s.config_served_at == 60.0 for s in fleet.servers)
    view = deployment_status(snapshot_for_status(fleet.deployments[0], cfg, 60.0), cfg.fleet, 60.0)
    assert view.lifecycle == LOADING
    # A host config.yaml already names is not built from an approval.
    fleet.approvals["127.0.0.1"] = Approval("127.0.0.1", ("qwen3-8b",), 65.0)
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
    # The approval's server is there, untouched: no probe was recorded.
    assert [(s.state, s.last_ok, s.public_port) for s in fleet.servers] == [(AWAITING, None, None)]


def test_adoption_leaves_an_approved_host_to_the_probe():
    """An approved host has its servers; adoption neither re-adds nor probes them (the lifecycle's probe does)."""
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
    (srv,) = fleet.servers
    assert (srv.state, srv.public_port, srv.config_served_at, srv.vllm_probed_at) == (AWAITING, None, None, None)
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


def test_probe_hits_both_vllm_ports_and_skips_the_rest():
    cfg = _cfg(
        {
            "198.51.100.8": [
                ["qwen3-8b", 8001, "a"],
                ["qwen3-8b", 8002, "b"],
            ]
        }
    )
    cfg.providers = edict(vast=edict(api_key="vast-test"))
    models = load_models(EXAMPLE)
    validate_fleet(cfg, models=models)
    fleet = Fleet.from_cfg(cfg, models=models)
    fleet.apply_port_map({"198.51.100.8": {"8001": 18001, "8002": 18002, "22": 19022}})
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    async def run():
        async with _client(handler) as client:
            await fleet.probe_all(client=client, now=10.0)

    asyncio.run(run())
    assert "http://198.51.100.8:18001/health" in seen
    assert "http://198.51.100.8:18002/health" in seen
    assert not any(":19022/" in url for url in seen)
    assert [s.state for s in fleet.servers] == [HEALTHY, HEALTHY]
    assert all(s.config_served_at is None for s in fleet.servers)


def test_missing_hostport_stays_awaiting_while_sibling_is_healthy():
    cfg = _cfg(
        {
            "198.51.100.8": [
                ["qwen3-8b", 8001, "a"],
                ["qwen3-8b", 8002, "b"],
            ]
        }
    )
    cfg.providers = edict(vast=edict(api_key="vast-test"))
    models = load_models(EXAMPLE)
    validate_fleet(cfg, models=models)
    fleet = Fleet.from_cfg(cfg, models=models)
    fleet.apply_port_map({"198.51.100.8": {"8001": 18001}})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async def run():
        async with _client(handler) as client:
            await fleet.probe_all(client=client, now=10.0)

    asyncio.run(run())
    by_port = {s.listen_port: s for s in fleet.servers}
    assert by_port[8001].state == HEALTHY
    assert by_port[8002].state == AWAITING
    assert by_port[8002].last_ok is None


def test_approved_siblings_are_probed_without_another_fetch():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    cfg.providers = edict(vast=edict(api_key="vast-test"))
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    fleet.approve_models("198.51.100.8", ["qwen3-8b", "gemma4-26b"])
    mapping = {"198.51.100.8": {"8001": 18001, "8002": 18002}}
    fleet._materialize_approved_hosts()
    fleet.apply_port_map(mapping)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async def run():
        async with _client(handler) as client:
            await fleet.probe_all(client=client, now=10.0)

    asyncio.run(run())
    assert sorted(s.listen_port for s in fleet.servers) == [8001, 8002]
    assert [s.state for s in fleet.servers] == [HEALTHY, HEALTHY]
    assert all(s.config_served_at is None for s in fleet.servers)


def test_approved_host_gains_the_sibling_port():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    fleet.approve_models("198.51.100.8", ["qwen3-8b", "gemma4-26b"])
    fleet.note_config_fetch("198.51.100.8", now=50.0)
    fleet.servers = [s for s in fleet.servers if s.listen_port == 8001]
    fleet.deployments = [d for d in fleet.deployments if d.vllm.listen_port == 8001]
    fleet._materialize_approved("198.51.100.8")
    assert sorted(s.listen_port for s in fleet.servers) == [8001, 8002]


def test_approval_drops_when_the_host_joins_config():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_models(EXAMPLE)
    fleet = _approvable(cfg, models)
    fleet.approve_models("198.51.100.8", ["qwen3-8b"])
    cfg.fleet.hosts["198.51.100.8"] = [["qwen3-8b", 8001]]
    fleet.note_unknown_config_fetch("203.0.113.9", now=40)
    assert "198.51.100.8" not in fleet.approvals


def test_optional_name_two_copies():
    cfg = _cfg(
        {
            "10.0.0.1": [
                ["qwen3-8b", 8001],
                ["qwen3-8b", 8002, "qwen3-8b-b"],
            ]
        }
    )
    models = load_models(EXAMPLE)
    validate_fleet(cfg, models=models)
    payload = config_for_host("10.0.0.1", models=models, cfg=cfg)
    assert [s["name"] for s in payload["servers"]] == ["qwen3-8b", "qwen3-8b-b"]
    assert [s["port"] for s in payload["servers"]] == [8001, 8002]


def test_duplicate_name():
    cfg = _cfg(
        {
            "10.0.0.1": [
                ["qwen3-8b", 8001],
                ["qwen3-8b", 8002],
            ]
        }
    )
    with pytest.raises(ValueError, match="duplicate name"):
        validate_fleet(cfg, models=load_models(EXAMPLE))


def test_legacy_boxes_need_the_shared_key_and_keep_their_ports():
    models = load_models(EXAMPLE)
    hosts = {
        "80.188.223.202": [["gemma4-26b", 10100]],
        "103.196.86.105": [["qwen3.8-27b", 8004, "qwen3.8-27b", 15602]],
        "213.173.110.78": [["qwen3-8b", 8003]],
    }
    missing = _cfg(hosts)
    with pytest.raises(ValueError, match="selfhost_api_key"):
        validate_fleet(missing, models=models)
    cfg = _cfg(hosts)
    cfg.fleet.selfhost_api_key = "sk-shared"
    cfg.providers = edict(vast=edict(api_key="vast-test"))
    validate_fleet(cfg, models=models)
    fleet = Fleet.from_cfg(cfg, models=models)
    fleet.apply_port_map({"80.188.223.202": {"10100": 40000, "22": 19022}})
    by_host = {s.host: s for s in fleet.servers}
    assert by_host["80.188.223.202"].public_port == 40000
    assert by_host["103.196.86.105"].public_port == 15602
    with pytest.raises(ValueError, match="not one of"):
        validate_fleet(_cfg({"10.0.0.1": [["qwen3-8b", 8004]]}), models=models)


def test_the_bearer_is_minted_once_with_the_intent():
    """One rule for the bearer a box runs with, applied when the intent is minted.

    Shared for LEGACY_SERVERS, derived from the salt for every other box.
    The server carries it, so the probe asks /v1/models with it and the
    executor registers it; neither derives anything.
    """
    from llmao.proxy.client import LiteLLMBackend

    cfg = _cfg({"80.188.223.202": [["gemma4-26b", 10100]], "10.0.0.1": [["qwen3-8b", 8001]]})
    cfg.fleet.selfhost_api_key = "sk-shared"
    cfg.litellm = edict(base_url="http://127.0.0.1:4000", master_key="sk-m", request_timeout_s=5)
    models = load_models(EXAMPLE)
    validate_fleet(cfg, models=models)
    fleet = Fleet.from_cfg(cfg, models=models)
    legacy, other = fleet.deployments
    assert legacy.vllm.api_key == "sk-shared"
    assert other.vllm.api_key == derive_vllm_api_key("test-vllm-salt", "10.0.0.1", 8001)
    assert vllm_api_key(cfg, "213.173.110.78", 8003) == "sk-shared"

    # The executor reads the server's key, and has no answer off our boxes.
    backend = LiteLLMBackend(cfg, fleet)
    assert backend.deployments._working_api_key(legacy) == "sk-shared"
    assert backend.deployments._working_api_key(edict(self_hosted=False, vllm=None)) is None
    asyncio.run(backend.aclose())


def test_served_window_is_asked_for_with_the_working_key():
    """The /v1/models read on the edge into Healthy sends what the box accepts.

    It used to send the derived key to every box, so a legacy box answered
    401 on every pass and its context window stayed unknown for good.
    """
    cfg = _cfg({"80.188.223.202": [["gemma4-26b", 10100]]})
    cfg.fleet.selfhost_api_key = "sk-shared"
    models = load_models(EXAMPLE)
    validate_fleet(cfg, models=models)
    fleet = Fleet.from_cfg(cfg, models=models)
    fleet.apply_port_map({"80.188.223.202": {"10100": 40000}})
    (srv,) = fleet.servers
    bearers = []

    class _Resp:
        def __init__(self, status_code, body=None):
            self.status_code = status_code
            self._body = body
            self.text = "OK"

        def json(self):
            return self._body

    class _Client:
        async def get(self, url, headers=None):
            if url.endswith("/health"):
                return _Resp(200)
            bearers.append((headers or {}).get("Authorization"))
            if headers and headers.get("Authorization") == "Bearer sk-shared":
                return _Resp(200, {"data": [{"max_model_len": 8192}]})
            return _Resp(401)

    asyncio.run(fleet.probe_all(client=_Client(), now=1.0))
    assert bearers == ["Bearer sk-shared"]
    assert srv.observed_max_model_len == 8192


def test_listen_port_outside_the_set_fails():
    cfg = _cfg({"10.0.0.1": [["qwen3-8b", 8004]]})
    with pytest.raises(ValueError, match="not one of 8001, 8002, 8003"):
        validate_fleet(cfg, models=load_models(EXAMPLE))


def test_fourth_server_fails():
    cfg = _cfg(
        {
            "10.0.0.1": [
                ["qwen3-8b", 8001, "a"],
                ["qwen3-8b", 8002, "b"],
                ["gemma4-26b", 8003, "c"],
                ["qwen3-8b", 8001, "d"],
            ]
        }
    )
    with pytest.raises(ValueError, match="at most 3"):
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
    cfg = _cfg({"10.0.0.1": [["nope", 8001]]})
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


def test_the_payload_carries_the_salt_hmac():
    """The payload carries the salt HMAC, not the legacy shared bearer."""
    cfg = _cfg({"10.0.0.1": [["qwen3-8b", 8003]]})
    cfg.fleet.selfhost_api_key = "sk-fleet"
    models = load_models(EXAMPLE)  # definitions carry no api_key
    payload = config_for_host("10.0.0.1", models=models, cfg=cfg)
    assert payload["servers"][0]["api_key"] == derive_vllm_api_key("test-vllm-salt", "10.0.0.1", 8003)
    assert payload["servers"][0]["api_key"] != "sk-fleet"


def test_the_payload_requires_the_salt():
    """A fleet config that never defines the salt fails at attribute access."""
    cfg = edict(
        {
            "fleet": {"hosts": {"10.0.0.1": [["qwen3-8b", 8003]]}, **FLEET_KNOBS},
            "models_path": str(EXAMPLE),
        }
    )
    with pytest.raises(AttributeError):
        config_for_host("10.0.0.1", models=load_models(EXAMPLE), cfg=cfg)


def test_a_fourth_element_warns_seeds_the_port_and_yields_to_the_listing(caplog):
    """A stale pinned row is a warning at load, not a refusal, and the listing wins over it.

    The seed keeps a RunPod box dialable from startup until its listing is
    first read; after that the listing's port is the port, as for Vast.
    """
    cfg = _cfg({"1.2.3.4": [["qwen3-8b", 8003, None, 15601]]})
    with caplog.at_level("WARNING", logger="llmao.fleet"):
        fleet = Fleet.from_cfg(cfg, models=load_models(EXAMPLE))
    assert "fleet.hosts.1.2.3.4[0]: the fourth element (public port 15601) is deprecated" in caplog.text
    srv = fleet.servers[0]
    assert srv.public_port == 15601
    assert srv.api_base == "http://1.2.3.4:15601/v1"

    # A map that knows nothing about this host leaves the seed; one that does, replaces it.
    fleet.apply_port_map({})
    assert srv.public_port == 15601
    fleet.apply_port_map({"1.2.3.4": {"8003": 12711}})
    assert srv.public_port == 12711


def test_a_port_follows_the_provider_map():
    cfg = _cfg({"1.2.3.4": [["qwen3-8b", 8003]]})
    fleet = Fleet.from_cfg(cfg, models=load_models(EXAMPLE))
    srv = fleet.servers[0]
    assert srv.public_port == 8003, "no GPU provider configured: a local fleet, public is listen"
    fleet.apply_port_map({"1.2.3.4": {"8003": 12711}})
    assert srv.public_port == 12711


def test_a_box_serves_the_name_litellm_asks_for():
    """vLLM's default name is the Hugging Face id; LiteLLM asks for ours.

    Three boxes served Qwen/Qwen3-8B while LiteLLM asked for qwen3-8b-bf16:
    /health passed and every request was a 404.
    """
    models = load_models(EXAMPLE)
    hosts = {}
    for i, model in enumerate(m for m in models if vllm_recipe(m) is not None):
        hosts[f"10.0.0.{i + 1}"] = [[model.model_name, 8001]]
    cfg = _cfg(hosts)
    validate_fleet(cfg, models=models)
    by_name = {m.model_name: m for m in models}
    for host, rows in hosts.items():
        (server,) = config_for_host(host, models=models, cfg=cfg)["servers"]
        # LiteLLM sends litellm_params.model with its provider prefix removed.
        asked = str(by_name[rows[0][0]].litellm_params.model).split("/", 1)[1]
        assert server["served_model_name"] == asked
        assert server["served_model_name"] != server["model"]


def test_the_box_is_told_the_kv_cache_its_context_needs():
    models = load_models(EXAMPLE)
    cfg = _cfg({"10.0.0.1": [["qwen3-8b", 8001]]})
    validate_fleet(cfg, models=models)
    (server,) = config_for_host("10.0.0.1", models=models, cfg=cfg)["servers"]
    assert server["kv_bytes_per_token"] == 73728
    assert server["max_model_len"] == 40960

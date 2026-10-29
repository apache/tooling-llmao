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

"""provisioning.py: the payload a box installs, rendered from its IntendedDeployments."""

import asyncio
import pathlib

import asfquart
from easydict import EasyDict as edict  # noqa: N813

from llmao.fleet import HOST_HEADER, Fleet
from llmao.intent import HOSTS, vllm_intent
from llmao.models import load_models
from llmao.provisioning import config_for_host, server_json
from tests.test_fleet import EXAMPLE, _cfg
from tests.test_intent import _by_name

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _app():
    if asfquart.APP is None:
        asfquart.construct("llmao", app_dir=str(ROOT), cfg_file="config.yaml.example", oauth=False, force_login=False)
    import api  # noqa: F401  (binds GET /vllm/config on the app at import)

    return asfquart.APP


def test_the_config_route_serves_members_and_approved_hosts_and_404s_a_stranger(monkeypatch):
    """GET /vllm/config end to end: the fleet key, the claimed host, the fleet's approvals."""
    app = _app()
    cfg = _cfg({"10.0.0.1": [["gemma4-26b", 8001]]})
    cfg.fleet.key = "test-fleet-key"
    models = load_models(EXAMPLE)
    fleet = Fleet(cfg=cfg, servers=[], models={m.model_name: m for m in models})
    fleet.approve_models("198.51.100.8", ["qwen3-8b"])
    monkeypatch.setattr(app, "cfg", cfg)
    monkeypatch.setattr(app, "models", models, raising=False)
    monkeypatch.setattr(app, "fleet", fleet, raising=False)
    client = app.test_client()

    async def fetch(host, key="test-fleet-key"):
        response = await client.get("/vllm/config", headers={"Authorization": f"Bearer {key}", HOST_HEADER: host})
        return response.status_code, await response.get_json()

    status, body = asyncio.run(fetch("10.0.0.1"))
    assert status == 200
    assert [(s["name"], s["port"]) for s in body["servers"]] == [("gemma4-26b", 8001)]
    assert fleet.provisioning.fetches["10.0.0.1"].at > 0

    status, body = asyncio.run(fetch("198.51.100.8"))
    assert status == 200
    assert [(s["name"], s["port"]) for s in body["servers"]] == [("qwen3-8b", 8001)]

    status, body = asyncio.run(fetch("192.0.2.9"))
    assert status == 404
    assert "192.0.2.9" in fleet.provisioning.unknown

    status, _ = asyncio.run(fetch("10.0.0.1", key="wrong"))
    assert status == 403


def test_server_json_is_the_listen_port_and_only_the_declared_requirements():
    cfg = _cfg({})
    model = edict(
        model_name="m",
        litellm_params=edict(model="hosted_vllm/m"),
        model_info=edict(vllm=edict(model="org/m", args=["--x"], gpu_memory_utilization=0.5)),
    )
    body = server_json(vllm_intent(cfg, "10.0.0.1", 8001, "box-m", model, origin=HOSTS, public_port=41234))
    assert body == edict(
        name="box-m",
        model="org/m",
        served_model_name="m",
        host="10.0.0.1",
        port=8001,
        api_key=body.api_key,
        args=["--x"],
        gpu_memory_utilization=0.5,
    )
    assert body.api_key.startswith("sk-vllm-")
    assert "max_model_len" not in body and "vram_gb" not in body


def test_config_for_host_renders_every_row_and_normalizes_the_host():
    cfg = _cfg({"10.0.0.1": [["qwen3-8b", 8003, "box-q"], ["gemma4-26b", 8001]]})
    payload = config_for_host("::ffff:10.0.0.1", models=list(_by_name().values()), cfg=cfg)
    assert payload.host == "10.0.0.1"
    assert [(s.name, s.port) for s in payload.servers] == [("box-q", 8003), ("gemma4-26b", 8001)]
    assert payload.servers[0].model == "Qwen/Qwen3-8B-FP8"
    assert EXAMPLE.is_file()

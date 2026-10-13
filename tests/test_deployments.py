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

"""Health-gated /model/new and /model/delete (mocked LiteLLM HTTP)."""

import asyncio

from easydict import EasyDict as edict  # noqa: N813

from llmao.fleet import Fleet, FleetDeployment
from llmao.model_status import HEALTHY, UNHEALTHY
from llmao.proxy.deployments import DeploymentManager
from tests.fake_transport import FakeTransport
from tests.test_fleet_health import _server


def _manager(fleet, answer=None):
    """A DeploymentManager over a stand-in LiteLLM that answers every call with `answer`."""
    cfg = edict(fleet=edict())
    return DeploymentManager(cfg, fleet, FakeTransport(lambda call: answer))


def _paths(manager):
    return [c.path for c in manager._transport.calls]


def test_deployment_body_from_models():
    s = _server()
    models = {
        "gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b")),
    }
    fleet = Fleet(None, [s], models=models)
    body = _manager(fleet).deployment_body(fleet.deployments[0])
    assert body["model_name"] == "gemma4-26b"
    assert body["litellm_params"]["model"] == "hosted_vllm/gemma4-26b"
    # api_base keeps the /v1 LiteLLM needs (it appends /chat/completions to it);
    # asf_api_base is the normalised comparison identity, suffix and all.
    assert body["litellm_params"]["api_base"] == "http://10.0.0.1:8001/v1"
    assert body["litellm_params"]["api_key"] == "sk-x"
    assert body["model_info"]["asf_api_base"] == "http://10.0.0.1:8001"
    assert body["model_info"]["self_hosted"] is True
    # in the group the pretend projects are limited to
    assert body["model_info"]["access_groups"] == ["llmao:self-hosted"]


def test_sync_selfhost_posts_new_when_serving():
    s = _server()
    s.state = HEALTHY
    models = {
        "gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b")),
    }
    fleet = Fleet(None, [s], models=models)
    be = _manager(fleet, {"data": []})
    asyncio.run(be.sync_selfhost())
    last = be._transport.calls[-1]
    assert (last.method, last.path) == ("POST", "model/new")
    assert fleet.deployments[0].in_litellm is True


def test_sync_selfhost_deletes_when_down():
    s = _server()
    s.state = UNHEALTHY
    models = {
        "gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b")),
    }
    fleet = Fleet(None, [s], models=models)
    fleet.deployments[0].in_litellm = True
    be = _manager(fleet, _info("10.0.0.1", 8001, model_id="abc"))
    asyncio.run(be.sync_selfhost())
    assert fleet.deployments[0].in_litellm is False


def _info(host: str, port: int, *, model_id: str | None = None):
    row = {
        "model_name": "gemma4-26b",
        "litellm_params": {"api_base": f"http://{host}:{port}/v1"},
    }
    if model_id is not None:
        row["model_info"] = {"id": model_id, "asf_api_base": f"http://{host}:{port}"}
    return {"data": [row]}


def test_sync_selfhost_does_not_repost_the_same_deployment():
    """Listen port and Vast HostPort are one deployment. Either row means it is registered."""
    s = _server(host="80.188.223.202", listen_port=10100, public_port=12711)
    s.state = HEALTHY
    models = {"gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b"))}
    for port in (12711, 10100):
        fleet = Fleet(None, [s], models=models)
        fleet.deployments[0].in_litellm = False
        be = _manager(fleet, _info("80.188.223.202", port))
        asyncio.run(be.sync_selfhost())
        assert "model/new" not in _paths(be)
        assert fleet.deployments[0].in_litellm is True


def test_other_port_is_not_this_deployment():
    s = _server(host="10.1.2.3", listen_port=8001, public_port=18001)
    s.state = HEALTHY
    models = {"gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b"))}
    fleet = Fleet(None, [s], models=models)
    be = _manager(fleet, _info("10.1.2.3", 19999))
    asyncio.run(be.sync_selfhost())
    assert "model/new" in _paths(be)


def test_ensure_commercial():
    dep = FleetDeployment.from_commercial(
        edict(
            model_name="paid-chat",
            litellm_params=edict(model="openai/gpt-4", api_base="https://api.openai.com/v1"),
            model_info=edict(),
        )
    )
    models = {
        "paid-chat": edict(
            litellm_params=edict(model="openai/gpt-4", api_base="https://api.openai.com/v1"),
        )
    }
    fleet = Fleet(None, [], deployments=[dep], models=models)
    be = _manager(fleet, {"data": []})
    # bought in, so in no access group: a pretend project's key cannot reach it
    assert dep.access_groups == set()
    assert "access_groups" not in be.deployment_body(dep)["model_info"]
    asyncio.run(be.ensure_commercial())
    assert _paths(be) == ["model/new"]
    assert dep.in_litellm is True

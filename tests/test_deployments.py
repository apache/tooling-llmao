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

from llmao.fleet import ORDER_SELF_HOSTED, ORDER_SPEND, Fleet, FleetDeployment
from llmao.model_status import HEALTHY, UNHEALTHY
from llmao.providers import ProviderDeployment
from llmao.proxy.deployments import DeploymentManager
from tests.fake_transport import FakeTransport
from tests.test_fleet import FLEET_KNOBS
from tests.test_fleet_health import _server


def _manager(fleet, answer=None, *, cfg=None):
    """A DeploymentManager over a stand-in LiteLLM that answers every call with `answer`.

    `answer` is a body for every call, or a function of the call.
    """
    cfg = cfg or edict(fleet=edict())
    respond = answer if callable(answer) else (lambda call: answer)
    return DeploymentManager(cfg, fleet, FakeTransport(respond))


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
    # Our boxes first: LiteLLM uses the lowest order among healthy deployments.
    assert body["litellm_params"]["order"] == ORDER_SELF_HOSTED
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


XAI = ProviderDeployment("xai", "grok-4", "xai/grok-4", "https://api.x.ai/v1")
OR_GROK = ProviderDeployment("openrouter", "grok-4", "openrouter/x-ai/grok-4", "https://openrouter.ai/api/v1")
OR_GEMMA = ProviderDeployment("openrouter", "gemma", "openrouter/google/gemma", "https://openrouter.ai/api/v1")
ACCOUNTS = edict(
    fleet=edict(FLEET_KNOBS),
    providers=edict(
        xai=edict(api_key="xai-test", api_base="https://api.x.ai/v1", models=edict()),
        openrouter=edict(api_key="sk-or-test", api_base="https://openrouter.ai/api/v1", models=edict()),
    ),
)


def _bought(*pds):
    deps = [FleetDeployment.from_provider(pd) for pd in pds]
    models = {pd.model_name: edict(model_name=pd.model_name, model_info=edict()) for pd in pds}
    return Fleet(None, [], deployments=deps, models=models), deps


def test_a_provider_deployment_is_built_from_config():
    fleet, (dep,) = _bought(XAI)
    body = _manager(fleet, cfg=ACCOUNTS).deployment_body(dep)
    assert body["model_name"] == "grok-4"
    assert body["litellm_params"] == {
        "model": "xai/grok-4",
        "api_base": "https://api.x.ai/v1",
        "api_key": "xai-test",
        "order": ORDER_SPEND,
    }
    # bought in, so in no access group: a pretend project's key cannot reach it
    assert dep.access_groups == set()
    assert "access_groups" not in body["model_info"]
    assert ORDER_SPEND > ORDER_SELF_HOSTED


def test_ensure_providers_registers_each_one_once():
    fleet, deps = _bought(XAI, OR_GROK)
    be = _manager(fleet, {"data": []}, cfg=ACCOUNTS)
    asyncio.run(be.ensure_providers())
    posts = [c.json for c in be._transport.calls if c.path == "model/new"]
    assert [p["litellm_params"]["api_key"] for p in posts] == ["xai-test", "sk-or-test"]
    assert all(d.in_litellm for d in deps)


def test_a_restart_finds_the_registration_already_there():
    """LiteLLM keeps a second copy if asked, so presence is checked first."""
    fleet, (dep,) = _bought(XAI)
    rows = {"data": [{"model_name": "grok-4", "litellm_params": {"api_base": "https://api.x.ai/v1"}}]}
    be = _manager(fleet, rows, cfg=ACCOUNTS)
    asyncio.run(be.ensure_providers())
    assert "model/new" not in _paths(be)
    assert dep.in_litellm is True


def test_two_models_on_one_account_are_not_skew():
    """OpenRouter serves both under one api_base; neither is the other's mismatch."""
    fleet, (grok, gemma) = _bought(OR_GROK, OR_GEMMA)
    base = "https://openrouter.ai/api/v1"
    info = {
        "data": [
            {"model_name": "grok-4", "litellm_params": {"api_base": base}},
            {"model_name": "gemma", "litellm_params": {"api_base": base}},
        ]
    }
    be = _manager(fleet, info, cfg=ACCOUNTS)
    asyncio.run(be.check_config_skew())
    assert grok.in_litellm and gemma.in_litellm
    assert not grok.config_mismatch and not gemma.config_mismatch
    assert fleet.extra_litellm == []


def test_health_on_a_shared_api_base_is_per_model():
    fleet, (grok, gemma) = _bought(OR_GROK, OR_GEMMA)
    base = "https://openrouter.ai/api/v1"
    health = {
        "healthy_endpoints": [{"api_base": base, "model": "openrouter/x-ai/grok-4"}],
        "unhealthy_endpoints": [{"api_base": base, "model": "openrouter/google/gemma"}],
    }
    asyncio.run(_manager(fleet, health, cfg=ACCOUNTS).check_health_skew())
    assert grok.litellm_healthy is True
    assert gemma.litellm_healthy is False

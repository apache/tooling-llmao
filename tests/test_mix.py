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

from easydict import EasyDict

from llmao.fleet import Fleet, FleetDeployment, VllmServer
from llmao.litellm_client import LiteLLMBackend
from tests.test_fleet_health import _server


def _backend(fleet):
    cfg = EasyDict(
        litellm=EasyDict(
            base_url="http://127.0.0.1:9",
            master_key="sk-x",
            request_timeout_s=1,
        ),
        fleet=EasyDict(),
    )
    return LiteLLMBackend(cfg, fleet)


def test_deployment_body_from_models():
    s = _server()
    models = {
        "gemma4-26b": EasyDict(litellm_params=EasyDict(model="hosted_vllm/gemma4-26b")),
    }
    fleet = Fleet(None, [s], models=models)
    body = _backend(fleet).deployment_body(fleet.deployments[0])
    assert body["model_name"] == "gemma4-26b"
    assert body["litellm_params"]["model"] == "hosted_vllm/gemma4-26b"
    # api_base keeps the /v1 LiteLLM needs (it appends /chat/completions to it);
    # asf_api_base is the normalised comparison identity, suffix and all.
    assert body["litellm_params"]["api_base"] == "http://10.0.0.1:8001/v1"
    assert body["litellm_params"]["api_key"] == "sk-x"
    assert body["model_info"]["asf_api_base"] == "http://10.0.0.1:8001"
    assert body["model_info"]["self_hosted"] is True


def test_sync_selfhost_posts_new_when_serving():
    s = _server()
    s.state = VllmServer.SERVING
    models = {
        "gemma4-26b": EasyDict(litellm_params=EasyDict(model="hosted_vllm/gemma4-26b")),
    }
    fleet = Fleet(None, [s], models=models)
    be = _backend(fleet)
    calls = []

    async def _req(method, path, **kw):
        calls.append((method, path, kw.get("json")))

        class R:
            status_code = 200

            def raise_for_status(self):
                return None

        return R()

    be._request = _req
    asyncio.run(be.sync_selfhost())
    assert calls[0][0] == "POST" and calls[0][1] == "model/new"
    assert fleet.deployments[0].in_litellm is True


def test_sync_selfhost_deletes_when_down():
    s = _server()
    s.state = VllmServer.DOWN
    models = {
        "gemma4-26b": EasyDict(litellm_params=EasyDict(model="hosted_vllm/gemma4-26b")),
    }
    fleet = Fleet(None, [s], models=models)
    fleet.deployments[0].in_litellm = True
    be = _backend(fleet)

    async def _req(method, path, **kw):
        class R:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": [
                        {
                            "model_name": "gemma4-26b",
                            "model_info": {
                                "id": "abc",
                                "asf_api_base": "http://10.0.0.1:8001",
                            },
                        }
                    ]
                }

        return R()

    be._request = _req
    asyncio.run(be.sync_selfhost())
    assert fleet.deployments[0].in_litellm is False


def test_ensure_commercial():
    dep = FleetDeployment.from_commercial(
        EasyDict(
            model_name="paid-chat",
            litellm_params=EasyDict(model="openai/gpt-4", api_base="https://api.openai.com/v1"),
            model_info=EasyDict(self_hosted=False),
        )
    )
    models = {
        "paid-chat": EasyDict(
            litellm_params=EasyDict(model="openai/gpt-4", api_base="https://api.openai.com/v1"),
        )
    }
    fleet = Fleet(None, [], deployments=[dep], models=models)
    be = _backend(fleet)
    calls = []

    async def _req(method, path, **kw):
        calls.append(path)

        class R:
            status_code = 200

            def raise_for_status(self):
                return None

        return R()

    be._request = _req
    asyncio.run(be.ensure_commercial())
    assert calls == ["model/new"]
    assert dep.in_litellm is True

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

"""FleetDeployment: commercial + vLLM intended backends."""

from easydict import EasyDict

from llmao.fleet import Fleet
from llmao.models import load_models
from tests.test_fleet import EXAMPLE, FLEET_KNOBS, _cfg
from tests.test_fleet_health import _server


def test_from_cfg_one_deployment_per_vllm():
    fleet = Fleet.from_cfg(_cfg({"127.0.0.1": [["gemma4-26b", 8001]]}), models=load_models(EXAMPLE))
    assert len(fleet.servers) == 1
    assert len(fleet.deployments) == 1
    dep = fleet.deployments[0]
    assert dep.self_hosted is True
    assert dep.vllm is fleet.servers[0]
    assert dep.api_base == "http://127.0.0.1:8001/v1"


def test_commercial_deployment_from_catalog():
    models = list(load_models(EXAMPLE))
    models.append(
        EasyDict(
            model_name="paid-chat",
            litellm_params=EasyDict(model="openai/gpt-4", api_base="https://api.openai.com/v1"),
            model_info=EasyDict(self_hosted=False, license="proprietary"),
        )
    )
    cfg = EasyDict(
        {
            "fleet": {"hosts": {"127.0.0.1": [["gemma4-26b", 8001]]}, **FLEET_KNOBS},
            "models_path": str(EXAMPLE),
        }
    )
    fleet = Fleet.from_cfg(cfg, models=models)
    names = [d.model_name for d in fleet.deployments]
    assert "gemma4-26b" in names
    assert "paid-chat" in names
    paid = next(d for d in fleet.deployments if d.model_name == "paid-chat")
    assert paid.self_hosted is False
    assert paid.vllm is None
    assert paid.api_base == "https://api.openai.com/v1"


def test_auto_deployments_from_servers_ctor():
    s = _server()
    fleet = Fleet(cfg=None, servers=[s])
    assert len(fleet.deployments) == 1
    assert fleet.deployments[0].vllm is s

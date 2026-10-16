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

"""FleetDeployment: our boxes (fleet.hosts) and providers (config.yaml providers)."""

import re

import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.fleet import Fleet, validate_fleet
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


GROK = edict(model_name="grok-4", model_info=edict(license="proprietary"))


def _with_providers(providers):
    models = [*load_models(EXAMPLE), GROK]
    cfg = edict(
        {
            "fleet": {
                "hosts": {"127.0.0.1": [["gemma4-26b", 8001]]},
                "vllm_api_salt": "test-vllm-salt",
                **FLEET_KNOBS,
            },
            "models_path": str(EXAMPLE),
            "providers": providers,
        }
    )
    return cfg, models


def _accounts():
    return {
        "xai": {"api_key": "xai-test", "api_base": "https://api.x.ai/v1", "models": {"grok-4": "xai/grok-4"}},
        "openrouter": {
            "api_key": "sk-or-test",
            "api_base": "https://openrouter.ai/api/v1",
            "models": {"grok-4": "openrouter/x-ai/grok-4", "gemma4-26b": "openrouter/google/gemma-4-26b-a4b-it"},
        },
    }


def test_where_a_model_is_served_comes_from_its_deployments():
    """grok-4 from two providers; gemma4 from our box and from one of them."""
    cfg, models = _with_providers(_accounts())
    validate_fleet(cfg, models=models)
    fleet = Fleet.from_cfg(cfg, models=models)
    by_name = {d.name: d for d in fleet.deployments}
    assert set(by_name) == {"gemma4-26b", "grok-4@xai", "grok-4@openrouter", "gemma4-26b@openrouter"}

    ours = by_name["gemma4-26b"]
    assert ours.self_hosted and ours.vllm is not None and ours.provider is None

    bought = by_name["gemma4-26b@openrouter"]
    assert bought.self_hosted is False
    assert bought.vllm is None
    assert bought.provider == "openrouter"
    assert bought.litellm_model == "openrouter/google/gemma-4-26b-a4b-it"
    assert bought.api_base == "https://openrouter.ai/api/v1"
    # Served both ways, so its dollars are not all an estimate.
    assert fleet.cost_basis("gemma4-26b") is None


def test_no_providers_section_means_no_provider_deployments():
    cfg, models = _with_providers(None)
    del cfg["providers"]
    validate_fleet(cfg, models=models)
    assert all(d.provider is None for d in Fleet.from_cfg(cfg, models=models).deployments)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p.xai.pop("api_key"), "providers.xai.api_key is required"),
        (lambda p: p.xai.update(api_key="CHANGE_ME_XAI"), "providers.xai.api_key is required"),
        (lambda p: p.xai.update(api_base="api.x.ai"), "providers.xai.api_base must be an http"),
        (lambda p: p.xai.update(models=["grok-4"]), "providers.xai.models must be a mapping"),
        (lambda p: p.xai.models.update(missing="xai/x"), "missing is not in models.yaml"),
        (lambda p: p.xai.models.update({"grok-4": ""}), "needs the provider's model"),
    ],
)
def test_providers_are_validated_at_startup(change, message):
    cfg, models = _with_providers(_accounts())
    change(cfg.providers)
    with pytest.raises(ValueError, match=re.escape(message)):
        validate_fleet(cfg, models=models)


def test_auto_deployments_from_servers_ctor():
    s = _server()
    fleet = Fleet(cfg=None, servers=[s])
    assert len(fleet.deployments) == 1
    assert fleet.deployments[0].vllm is s

"""FleetDeployment: commercial + vLLM intended backends."""
from easydict import EasyDict as edict

from llmao.fleet import Fleet
from llmao.models import load_model_list
from tests.test_fleet import EXAMPLE, FLEET_KNOBS, _cfg
from tests.test_fleet_health import _server


def test_from_cfg_one_deployment_per_vllm():
    fleet = Fleet.from_cfg(_cfg({"127.0.0.1": [["gemma4-26b", 8001]]}), models=load_model_list(EXAMPLE))
    assert len(fleet.servers) == 1
    assert len(fleet.deployments) == 1
    dep = fleet.deployments[0]
    assert dep.self_hosted is True
    assert dep.vllm is fleet.servers[0]
    assert dep.api_base == "http://127.0.0.1:8001"


def test_commercial_deployment_from_catalog():
    models = list(load_model_list(EXAMPLE))
    models.append(edict(
        model_name="paid-chat",
        litellm_params=edict(model="openai/gpt-4", api_base="https://api.openai.com/v1"),
        model_info=edict(self_hosted=False, license="proprietary"),
    ))
    cfg = edict({
        "fleet": {"hosts": {"127.0.0.1": [["gemma4-26b", 8001]]}, **FLEET_KNOBS},
        "models_path": str(EXAMPLE),
    })
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

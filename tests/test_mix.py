"""Health-gated /model/new and /model/delete (mocked LiteLLM HTTP)."""
import asyncio

from easydict import EasyDict as edict

from llmao.fleet import Fleet, FleetDeployment, VllmServer
from llmao.litellm_client import LiteLLMBackend
from tests.test_fleet_health import _server


def _backend(fleet):
    cfg = edict(
        litellm=edict(
            base_url="http://127.0.0.1:9",
            master_key="sk-x",
            request_timeout_s=1,
        ),
        fleet=edict(),
    )
    return LiteLLMBackend(cfg, fleet)


def test_deployment_body_from_catalog():
    s = _server()
    catalog = {
        "gemma4-26b": edict(litellm_params=edict(model="openai/gemma4-26b")),
    }
    fleet = Fleet(None, [s], catalog=catalog)
    body = _backend(fleet).deployment_body(fleet.deployments[0])
    assert body["model_name"] == "gemma4-26b"
    assert body["litellm_params"]["model"] == "openai/gemma4-26b"
    assert body["litellm_params"]["api_base"] == "http://10.0.0.1:8001"
    assert body["litellm_params"]["api_key"] == "sk-x"
    assert body["model_info"]["asf_api_base"] == "http://10.0.0.1:8001"
    assert body["model_info"]["self_hosted"] is True


def test_sync_selfhost_posts_new_when_serving():
    s = _server()
    s.state = VllmServer.SERVING
    catalog = {
        "gemma4-26b": edict(litellm_params=edict(model="openai/gemma4-26b")),
    }
    fleet = Fleet(None, [s], catalog=catalog)
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
    catalog = {
        "gemma4-26b": edict(litellm_params=edict(model="openai/gemma4-26b")),
    }
    fleet = Fleet(None, [s], catalog=catalog)
    fleet.deployments[0].in_litellm = True
    be = _backend(fleet)

    async def _req(method, path, **kw):
        class R:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"data": [{
                    "model_name": "gemma4-26b",
                    "model_info": {
                        "id": "abc",
                        "asf_api_base": "http://10.0.0.1:8001",
                    },
                }]}

        return R()

    be._request = _req
    asyncio.run(be.sync_selfhost())
    assert fleet.deployments[0].in_litellm is False


def test_ensure_commercial():
    dep = FleetDeployment.from_commercial(edict(
        model_name="paid-chat",
        litellm_params=edict(model="openai/gpt-4", api_base="https://api.openai.com/v1"),
        model_info=edict(self_hosted=False),
    ))
    catalog = {"paid-chat": edict(
        litellm_params=edict(model="openai/gpt-4", api_base="https://api.openai.com/v1"),
    )}
    fleet = Fleet(None, [], deployments=[dep], catalog=catalog)
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

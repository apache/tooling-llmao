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

from llmao.fleet import ORDER_SELF_HOSTED, ORDER_SPEND, ORPHAN_PASSES, Fleet, FleetDeployment
from llmao.model_status import AWAITING, HEALTHY, SKEW_AT_LISTEN_PORT, UNHEALTHY
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


def _litellm(rows=None):
    """A stand-in LiteLLM that remembers its deployments, so a read after a write sees the write.

    model/info answers with the rows; model/new appends the posted body with
    an id; model/delete removes the row with that id. Pass it to _manager.
    """
    state = edict(rows=list(rows or []), n=0)

    def answer(call):
        if call.path == "model/info":
            return {"data": list(state.rows)}
        if call.path == "model/new":
            state.n += 1
            row = dict(call.json)
            row["model_info"] = {**(row.get("model_info") or {}), "id": f"id-{state.n}"}
            state.rows.append(row)
            return {}
        if call.path == "model/delete":
            state.rows = [r for r in state.rows if (r.get("model_info") or {}).get("id") != call.json["id"]]
            return {}
        return {}

    return answer


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


def test_a_self_hosted_deployment_carries_no_price():
    """A price would count against team budgets; self-hosted use must never block."""
    s = _server()
    model = edict(
        litellm_params=edict(model="hosted_vllm/gemma4-26b"),
        pricing=edict(input_cost_per_token=1e-07, output_cost_per_token=1e-06),
    )
    fleet = Fleet(None, [s], models={"gemma4-26b": model})
    body = _manager(fleet).deployment_body(fleet.deployments[0])
    sent = {**body["litellm_params"], **body["model_info"]}
    assert not {"input_cost_per_token", "output_cost_per_token", "cost_basis"} & set(sent)


def test_sync_selfhost_posts_new_when_serving():
    s = _server()
    s.state = HEALTHY
    models = {
        "gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b")),
    }
    fleet = Fleet(None, [s], models=models)
    be = _manager(fleet, _litellm())
    asyncio.run(be.sync_selfhost())
    # Is it there?, the write, and the read that confirms the write.
    assert _paths(be) == ["model/info", "model/new", "model/info"]
    assert fleet.deployments[0].in_litellm is True


def test_presence_after_a_write_is_observed_not_assumed():
    """A LiteLLM that answers 200 to model/new and then does not list the deployment.

    The old code set in_litellm on the strength of the 200. Now the read that
    follows decides, so the next pass tries again instead of trusting a write
    LiteLLM lost.
    """
    s = _server()
    s.state = HEALTHY
    fleet = Fleet(None, [s], models={"gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b"))})
    be = _manager(fleet, {"data": []})
    asyncio.run(be.sync_selfhost())
    assert "model/new" in _paths(be)
    assert fleet.deployments[0].in_litellm is False


def test_a_failed_confirm_keeps_the_last_answer(caplog):
    """The re-read after a write failed: the write is assumed, as before, and the tick does not raise."""
    s = _server()
    s.state = HEALTHY
    fleet = Fleet(None, [s], models={"gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b"))})
    reads = []

    def answer(call):
        if call.path == "model/info":
            reads.append(call)
            return (500, {}) if len(reads) > 1 else {"data": []}
        return {}

    be = _manager(fleet, answer)
    with caplog.at_level("WARNING", logger="llmao.proxy.deployments"):
        asyncio.run(be.sync_selfhost())
    assert _paths(be) == ["model/info", "model/new", "model/info"]
    assert fleet.deployments[0].in_litellm is True
    assert "could not be re-read after a write" in caplog.text


def test_one_failing_deployment_does_not_stop_the_rest(caplog):
    """The first deployment raised; every one after it was never registered."""
    broken = _server(model_name="gone", name="gone", host="10.0.0.9")
    fine = [_server(host="10.0.0.1"), _server(host="10.0.0.2")]
    for s in (broken, *fine):
        s.state = HEALTHY
    # "gone" is not in models.yaml, so its body cannot be built.
    models = {"gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b"))}
    fleet = Fleet(None, [broken, *fine], models=models)
    be = _manager(fleet, _litellm())
    with caplog.at_level("ERROR", logger="llmao.proxy.deployments"):
        asyncio.run(be.sync_selfhost())
    posted = [c.json["litellm_params"]["api_base"] for c in be._transport.calls if c.path == "model/new"]
    assert posted == ["http://10.0.0.1:8001/v1", "http://10.0.0.2:8001/v1"]
    assert [d.in_litellm for d in fleet.deployments] == [False, True, True]
    # Says which deployment, so the log points at the cause.
    assert "add_deployment failed for gone@http://10.0.0.9:8001/v1" in caplog.text


def test_sync_selfhost_deletes_when_down():
    s = _server()
    s.state = UNHEALTHY
    models = {
        "gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b")),
    }
    fleet = Fleet(None, [s], models=models)
    fleet.deployments[0].in_litellm = True
    be = _manager(fleet, _litellm(_info("10.0.0.1", 8001, model_id="abc")["data"]))
    asyncio.run(be.sync_selfhost())
    # The id lookup, the delete, and the read that confirms it.
    assert _paths(be) == ["model/info", "model/delete", "model/info"]
    assert fleet.deployments[0].in_litellm is False


def test_sync_selfhost_deletes_a_box_no_provider_lists():
    """Awaiting, never probed, but gone: its deployment points at nothing."""
    s = _server()
    s.state = AWAITING
    s.unlisted = True
    models = {
        "gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b")),
    }
    fleet = Fleet(None, [s], models=models)
    fleet.deployments[0].in_litellm = True
    be = _manager(fleet, _litellm(_info("10.0.0.1", 8001, model_id="abc")["data"]))
    asyncio.run(be.sync_selfhost())
    assert fleet.deployments[0].in_litellm is False
    assert "model/delete" in _paths(be)


def test_a_serving_box_no_provider_lists_keeps_its_route():
    """The invariant remove_refusal states: a box that answers keeps its route.

    e1567cb deleted an unlisted server's route regardless of health, and a
    RunPod pod absent from the pod list while provisioning lost its route on
    alternating passes while serving normally.
    """
    s = _server()
    s.state = HEALTHY
    s.unlisted = True
    models = {"gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b"))}
    fleet = Fleet(None, [s], models=models)
    fleet.deployments[0].in_litellm = True
    be = _manager(fleet, _info("10.0.0.1", 8001, model_id="abc"))
    asyncio.run(be.sync_selfhost())
    assert fleet.deployments[0].in_litellm is True
    assert "model/delete" not in _paths(be)


def _orphan(model_id="orphan-1"):
    """A LiteLLM route at an address no deployment of ours claims."""
    fleet = Fleet(None, [], models={"gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b"))})
    return fleet, _info("198.51.100.9", 18490, model_id=model_id)


def test_an_orphan_route_is_deleted_once_it_has_persisted():
    """tooling-llmao#29: a host that changed address leaves its route behind.

    Routes are made on the transition into SERVING and dropped on the one into
    DOWN; a server removed from fleet.hosts makes neither, so check_config_skew
    is the only thing that sees the leftover. It used to only report it.
    """
    fleet, info = _orphan()
    be = _manager(fleet, info, cfg=edict(fleet=edict(FLEET_KNOBS)))
    for expected in range(1, ORPHAN_PASSES):
        asyncio.run(be.check_config_skew())
        assert [row.passes for row in fleet.extra_litellm] == [expected]
        assert "model/delete" not in _paths(be), "not on the strength of one pass"
    asyncio.run(be.check_config_skew())
    assert "model/delete" in _paths(be)
    sent = [c.json["id"] for c in be._transport.calls if c.path == "model/delete"]
    assert sent == ["orphan-1"]
    # A write, then the read that confirms it.
    assert _paths(be)[-2:] == ["model/delete", "model/info"]


def test_an_orphan_that_is_claimed_again_is_never_deleted():
    """A provider lookup that failed one pass must not cost a live route."""
    fleet, info = _orphan()
    # A callable answer, so what LiteLLM reports can change between passes.
    reply = {"body": info}
    be = _manager(fleet, lambda call: reply["body"], cfg=edict(fleet=edict(FLEET_KNOBS)))
    asyncio.run(be.check_config_skew())
    assert [row.passes for row in fleet.extra_litellm] == [1]
    # The route is gone from LiteLLM's answer: nothing is an extra this pass.
    reply["body"] = {"data": []}
    asyncio.run(be.check_config_skew())
    assert fleet.extra_litellm == []
    # Back again: the count starts over rather than resuming.
    reply["body"] = info
    for expected in range(1, ORPHAN_PASSES):
        asyncio.run(be.check_config_skew())
        assert [row.passes for row in fleet.extra_litellm] == [expected]
    assert "model/delete" not in _paths(be)


def test_an_orphan_with_no_litellm_id_is_reported_not_deleted(caplog):
    """LiteLLM answered without an id, so there is nothing to delete by."""
    fleet, info = _orphan(model_id=None)
    be = _manager(fleet, info, cfg=edict(fleet=edict(FLEET_KNOBS)))
    with caplog.at_level("WARNING", logger="llmao.proxy.deployments"):
        for _ in range(ORPHAN_PASSES + 1):
            asyncio.run(be.check_config_skew())
    assert "model/delete" not in _paths(be)
    assert "has no LiteLLM id" in caplog.text


def _info(host: str, port: int, *, model_id: str | None = None):
    row = {
        "model_name": "gemma4-26b",
        "litellm_params": {"api_base": f"http://{host}:{port}/v1"},
    }
    if model_id is not None:
        row["model_info"] = {"id": model_id, "asf_api_base": f"http://{host}:{port}"}
    return {"data": [row]}


def _listen_and_public():
    """A Vast-style server: listens on 10100, published on 12711."""
    s = _server(host="10.0.0.7", listen_port=10100, public_port=12711)
    s.state = HEALTHY
    models = {"gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b"))}
    return Fleet(None, [s], models=models)


def test_sync_selfhost_does_not_repost_the_same_deployment():
    fleet = _listen_and_public()
    be = _manager(fleet, _info("10.0.0.7", 12711))
    asyncio.run(be.sync_selfhost())
    assert "model/new" not in _paths(be)
    assert fleet.deployments[0].in_litellm is True


def test_a_deployment_at_the_listen_port_does_not_stand_in_for_the_reachable_one():
    """Off-box the listen port is unreachable, so the server is registered properly."""
    fleet = _listen_and_public()
    be = _manager(fleet, _info("10.0.0.7", 10100))
    asyncio.run(be.sync_selfhost())
    posted = [c.json["litellm_params"]["api_base"] for c in be._transport.calls if c.path == "model/new"]
    assert posted == ["http://10.0.0.7:12711/v1"]


def test_a_deployment_at_the_listen_port_is_reported_on_its_server():
    fleet = _listen_and_public()
    (dep,) = fleet.deployments
    info = {
        "data": [
            {"model_name": "gemma4-26b", "litellm_params": {"api_base": "http://10.0.0.7:12711/v1"}},
            {"model_name": "gemma4-26b", "litellm_params": {"api_base": "http://10.0.0.7:10100/v1"}},
        ]
    }
    be = _manager(fleet, info, cfg=edict(fleet=edict(FLEET_KNOBS)))
    asyncio.run(be.check_config_skew())
    assert dep.in_litellm is True
    assert dep.at_listen_port is True
    assert SKEW_AT_LISTEN_PORT in dep.skew
    # Once, on its server; not again as a stranger.
    assert fleet.extra_litellm == []


def test_where_the_ports_are_one_the_deployment_is_simply_right():
    """Local: no provider in between, the listen port is the public one."""
    s = _server(host="127.0.0.1", listen_port=8001, public_port=8001)
    s.state = HEALTHY
    fleet = Fleet(None, [s], models={"gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b"))})
    be = _manager(fleet, _info("127.0.0.1", 8001), cfg=edict(fleet=edict(FLEET_KNOBS)))
    asyncio.run(be.check_config_skew())
    assert fleet.deployments[0].in_litellm is True
    assert fleet.deployments[0].at_listen_port is False


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


def test_a_failed_registration_is_kept_for_the_page_until_one_succeeds():
    fleet, (dep,) = _bought(XAI)
    be = _manager(fleet, cfg=ACCOUNTS)

    async def refused(_dep):
        raise RuntimeError("LiteLLM said no")

    async def accepted(_dep):
        pass

    asyncio.run(be._each(refused, dep))
    assert dep.litellm_error == "LiteLLM said no"
    assert dep.litellm_error_at is not None
    asyncio.run(be._each(accepted, dep))
    assert dep.litellm_error == ""
    assert dep.litellm_error_at is None


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


def _unplaced():
    """A box whose public port is not known this pass, after an earlier pass knew it."""
    box = _server(host="10.0.0.5", listen_port=8001, public_port=None)
    box.state = HEALTHY
    fleet = Fleet(None, [box], models={"gemma4-26b": edict(litellm_params=edict(model="hosted_vllm/gemma4-26b"))})
    (dep,) = fleet.deployments
    dep.in_litellm = True
    dep.litellm_healthy = True
    dep.litellm_health_at = 1000.0
    return fleet, dep


def test_an_unknown_address_is_not_missing_from_litellm():
    fleet, dep = _unplaced()
    info = {
        "data": [
            # Its own deployment, on the public port nobody knows this pass.
            {"model_name": "gemma4-26b", "litellm_params": {"api_base": "http://10.0.0.5:41234/v1"}},
            # Somebody else's: a host the fleet does not have.
            {"model_name": "gemma4-26b", "litellm_params": {"api_base": "http://10.0.0.99:8001/v1"}},
        ]
    }
    be = _manager(fleet, info, cfg=edict(fleet=edict(FLEET_KNOBS)))
    asyncio.run(be.check_config_skew())
    assert dep.in_litellm is True
    assert [(x.host, x.port) for x in fleet.extra_litellm] == [("10.0.0.99", 8001)]


def test_an_unknown_address_is_not_found_on_its_listen_port():
    """Off-box the listen port is unreachable; finding it there proves nothing."""
    fleet, dep = _unplaced()
    dep.in_litellm = False
    info = {"data": [{"model_name": "gemma4-26b", "litellm_params": {"api_base": "http://10.0.0.5:8001/v1"}}]}
    be = _manager(fleet, info, cfg=edict(fleet=edict(FLEET_KNOBS)))
    asyncio.run(be.check_config_skew())
    assert dep.in_litellm is False


def test_an_unknown_address_keeps_its_last_litellm_health():
    """Wiping it would leave the deployment unjudged for four hours."""
    fleet, dep = _unplaced()
    be = _manager(fleet, {"healthy_endpoints": [], "unhealthy_endpoints": []}, cfg=edict(fleet=edict(FLEET_KNOBS)))
    asyncio.run(be.check_health_skew())
    assert dep.litellm_healthy is True
    assert dep.litellm_health_at == 1000.0

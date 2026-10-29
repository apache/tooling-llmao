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

"""intent.py: IntendedDeployment from fleet.hosts rows, approvals and provider lines; the bearer."""

import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.intent import (
    APPROVAL,
    HOSTS,
    PROVIDER,
    UnknownHostError,
    derive_vllm_api_key,
    host_intents,
    intended_deployments,
    parse_host_row,
    provider_intent,
)
from llmao.models import VllmRecipe, load_models
from tests.test_fleet import EXAMPLE, _cfg

# HMAC-SHA256("test-vllm-salt", "llmao.vllm.api_key\n203.0.113.10\n8001")
_GOLDEN = "sk-vllm-YU9F3Sgfr-gNOZKZHGc84-cbgua6IiDkhr3XlaX35Tc"


def test_golden_vector():
    assert derive_vllm_api_key("test-vllm-salt", "203.0.113.10", 8001) == _GOLDEN


def test_port_changes_key():
    a = derive_vllm_api_key("test-vllm-salt", "203.0.113.10", 8001)
    b = derive_vllm_api_key("test-vllm-salt", "203.0.113.10", 8002)
    assert a != b
    assert a.startswith("sk-vllm-")


def test_normalizes_v4mapped_host():
    assert derive_vllm_api_key("test-vllm-salt", "::ffff:203.0.113.10", 8001) == _GOLDEN


def test_the_salt_is_checked_at_startup_not_at_mint():
    """validate_fleet refuses a missing or placeholder salt; the mint then trusts it."""
    from llmao.fleet import validate_fleet

    models = load_models(EXAMPLE)
    for bad in (None, "", "  ", "CHANGE_ME_VLLM_API_SALT"):
        cfg = _cfg({"10.0.0.1": [["qwen3-8b", 8001]]})
        cfg.fleet.vllm_api_salt = bad
        with pytest.raises(ValueError, match="vllm_api_salt is missing or CHANGE_ME"):
            validate_fleet(cfg, models=models)
    cfg = _cfg({"10.0.0.1": [["qwen3-8b", 8001]]})
    del cfg.fleet["vllm_api_salt"]
    with pytest.raises(ValueError, match="vllm_api_salt is missing or CHANGE_ME"):
        validate_fleet(cfg, models=models)
    assert derive_vllm_api_key("CHANGE_ME_VLLM_API_SALT", "203.0.113.10", 8001).startswith("sk-vllm-")


def test_the_mint_still_refuses_a_bad_host_or_port():
    with pytest.raises(ValueError, match="host is empty"):
        derive_vllm_api_key("test-vllm-salt", "", 8001)
    with pytest.raises(ValueError, match="positive int"):
        derive_vllm_api_key("test-vllm-salt", "203.0.113.10", 0)


def test_host_row_still_reads_the_deprecated_fourth_element():
    """[model, port, name, public_port] loads: a stale row warns, it does not stop llmao.

    The fourth element pinned a RunPod box's public port while RunPod's
    mapping was not read. Both listings carry the port now; the element is
    read so the loader can warn and seed, and it goes away in config.
    """
    assert parse_host_row(["qwen3-8b", 8003], "h", 0) == ("qwen3-8b", 8003, "qwen3-8b", None)
    assert parse_host_row(["qwen3-8b", 8003, "b"], "h", 0) == ("qwen3-8b", 8003, "b", None)
    assert parse_host_row(["qwen3-8b", 8003, "b", 15601], "h", 0) == ("qwen3-8b", 8003, "b", 15601)


def test_host_row_null_name_reaches_the_fourth_slot():
    """null for name was how a port got pinned without renaming the server."""
    _, _, name, public = parse_host_row(["qwen3-8b", 8003, None, 15601], "h", 0)
    assert name == "qwen3-8b"  # falls back to the model name
    assert public == 15601


def test_host_row_rejects_bad_public_port():
    with pytest.raises(ValueError, match="public_port must be an int"):
        parse_host_row(["qwen3-8b", 8003, None, "nope"], "h", 0)
    with pytest.raises(ValueError, match="must be"):
        parse_host_row(["qwen3-8b", 8003, "b", 1, 2], "h", 0)


def _by_name():
    return {m.model_name: m for m in load_models(EXAMPLE)}


def test_a_host_row_is_one_intended_deployment_with_its_recipe_and_bearer():
    cfg = _cfg({"10.0.0.1": [["qwen3-8b", 8003, "box-a"]]})
    (one,) = host_intents(cfg, _by_name(), "::ffff:10.0.0.1")
    assert one.origin == HOSTS
    assert one.self_hosted
    assert (one.host, one.listen_port, one.name, one.model_name) == ("10.0.0.1", 8003, "box-a", "qwen3-8b")
    assert one.key.url == "http://10.0.0.1:8003"
    assert one.key.model_name == "qwen3-8b"
    assert one.api_key == derive_vllm_api_key("test-vllm-salt", "10.0.0.1", 8003)
    assert one.recipe == VllmRecipe.from_model(_by_name()["qwen3-8b"])
    assert one.recipe.hf_model == "Qwen/Qwen3-8B-FP8"
    assert one.provider is None and one.api_base is None
    assert one.public_port is None


def test_an_approval_takes_the_listen_ports_in_order():
    cfg = _cfg({"10.0.0.1": [["qwen3-8b", 8003]]})
    two = host_intents(cfg, _by_name(), "198.51.100.8", approved=["gemma4-26b", "qwen3-8b"])
    assert [(i.origin, i.listen_port, i.name) for i in two] == [
        (APPROVAL, 8001, "gemma4-26b"),
        (APPROVAL, 8002, "qwen3-8b"),
    ]
    with pytest.raises(ValueError, match="at most 3"):
        host_intents(cfg, _by_name(), "198.51.100.8", approved=["a", "b", "c", "d"])


def test_a_host_with_neither_rows_nor_approval_is_unknown():
    cfg = _cfg({"10.0.0.1": [["qwen3-8b", 8003]]})
    with pytest.raises(UnknownHostError):
        host_intents(cfg, _by_name(), "10.0.0.9")
    with pytest.raises(UnknownHostError):
        host_intents(cfg, _by_name(), "")


def test_a_provider_line_is_one_intended_deployment_keyed_by_its_base():
    one = provider_intent("openrouter", "grok-4", "openrouter/x-ai/grok-4", "https://openrouter.ai/api/v1/")
    assert one.origin == PROVIDER
    assert not one.self_hosted
    assert one.name == "grok-4@openrouter"
    assert one.key.url == "https://openrouter.ai/api"  # norm_base: no /v1, no trailing slash
    assert one.litellm_model == "openrouter/x-ai/grok-4"
    assert one.recipe is None and one.host is None and one.api_key == ""


def test_intended_deployments_is_rows_then_approvals_then_provider_lines():
    cfg = _cfg({"10.0.0.1": [["qwen3-8b", 8003]]})
    cfg.providers = edict(
        xai=edict(api_key="xai-test", api_base="https://api.x.ai/v1", models=edict({"qwen3-8b": "xai/qwen"})),
        vast=edict(api_key="vast-test"),
    )
    approved = {"198.51.100.8": ["gemma4-26b"], "10.0.0.1": ["gemma4-26b"], "10.0.0.2": []}
    out = intended_deployments(cfg, load_models(EXAMPLE), approved)
    # A member's stale approval is not a second intent for the same host; an
    # empty approval is none; a GPU host account serves nothing.
    assert [(i.origin, i.name) for i in out] == [
        (HOSTS, "qwen3-8b"),
        (APPROVAL, "gemma4-26b"),
        (PROVIDER, "qwen3-8b@xai"),
    ]


def test_a_recipe_leaves_undeclared_requirements_as_none():
    recipe = VllmRecipe.from_model(
        edict(
            model_name="m",
            litellm_params=edict(model="hosted_vllm/m"),
            model_info=edict(vllm=edict(model="org/m", args="--a --b", max_model_len=4096)),
        )
    )
    assert recipe.hf_model == "org/m"
    assert recipe.served_name == "m"
    assert recipe.args == ("--a", "--b")
    assert recipe.max_model_len == 4096
    assert recipe.vram_gb is None and recipe.kv_bytes_per_token is None
    assert VllmRecipe.from_model(edict(model_name="n", model_info=edict())) is None

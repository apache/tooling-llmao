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

"""The deployment key, frozen: docs/fleet-reconcile-design.md, migration step 1."""

import pytest

from llmao.reconcile import DeploymentKey, deployment_key, norm_base


def test_norm_base_strips_trailing_v1():
    """Why the normalisation exists.

    An older LiteLLM appended /v1/chat/completions to api_base, so a base
    that already ended in /v1 resolved to /v1/v1/... and 404'd at the origin.
    LiteLLM then reported an OpenAI authentication error naming
    platform.openai.com -- so the symptom pointed at the caller's key rather
    than at the route. This happened in production against the Fastly-fronted
    Gemma endpoint. The current LiteLLM appends only /chat/completions, so a
    /v1-suffixed base is what vLLM needs now; routes pushed under the old
    behaviour may lack it, and the comparison must match across both forms.
    """
    assert norm_base("https://llm.tooling.apache.org/v1") == "https://llm.tooling.apache.org"
    assert norm_base("https://llm.tooling.apache.org/v1/") == "https://llm.tooling.apache.org"
    assert norm_base("http://127.0.0.1:8003/v1") == "http://127.0.0.1:8003"


def test_norm_base_leaves_other_paths_alone():
    """Only a trailing /v1 is stripped -- not a path that merely contains it, and only one."""
    assert norm_base("http://127.0.0.1:8003") == "http://127.0.0.1:8003"
    assert norm_base("https://openrouter.ai/api/v1") == "https://openrouter.ai/api"
    assert norm_base("https://example.test/v1/proxy") == "https://example.test/v1/proxy"
    assert norm_base("https://example.test/v1/v1") == "https://example.test/v1"


def test_norm_base_host_case_and_mapped_ipv6():
    """Scheme and host are case-insensitive on the wire, so they are one key here.

    A peer that arrived as IPv4-mapped IPv6 is the same box as its IPv4 form.
    """
    assert norm_base("HTTPS://OpenRouter.ai/api/v1") == "https://openrouter.ai/api"
    assert norm_base("http://[::ffff:192.0.2.10]:8001/v1") == "http://192.0.2.10:8001"
    assert norm_base("http://[2001:db8::1]:8001") == "http://[2001:db8::1]:8001"


def test_norm_base_makes_skew_comparison_match():
    """The comparison this exists for.

    check_config_skew compares VllmServer.api_base against what LiteLLM reports.
    VllmServer.api_base ends in /v1; a deployment pushed before that, or a
    models.yaml row, may lack it. Without normalisation on both sides the
    same deployment never matches, and the skew check reports a missing
    deployment that is actually present.
    """
    assert norm_base("http://192.0.2.10:8003/v1") == norm_base("http://192.0.2.10:8003")


def test_norm_base_tolerates_what_is_not_a_url():
    assert norm_base(None) == ""
    assert norm_base("") == ""
    assert norm_base("  ") == ""
    assert norm_base("192.0.2.10:8001/v1") == "192.0.2.10:8001"


def test_box_key_is_the_listen_port():
    """A Vast remap changes the public port and nothing else; the key does not move."""
    key = deployment_key("gemma4-26b", host="192.0.2.10", listen_port=8001)
    assert key == DeploymentKey("http://192.0.2.10:8001", "gemma4-26b")
    assert key.url == "http://192.0.2.10:8001"
    assert key.model_name == "gemma4-26b"
    assert deployment_key("gemma4-26b", host="::ffff:192.0.2.10", listen_port="8001") == key


def test_box_key_matches_a_stamped_or_registered_base():
    """What LiteLLM holds, in any of its forms, normalises to the same bytes."""
    key = deployment_key("qwen3-8b", host="192.0.2.10", listen_port=8003)
    for form in ("http://192.0.2.10:8003/v1", "http://192.0.2.10:8003/", "HTTP://192.0.2.10:8003"):
        assert norm_base(form) == key.url


def test_legacy_pair_is_an_ordinary_key():
    """A legacy box on an odd listen port is keyed like any other: nothing remembers it is legacy."""
    key = deployment_key("gemma4-26b", host="192.0.2.20", listen_port=10100)
    assert key.url == "http://192.0.2.20:10100"


def test_sibling_ports_are_two_keys():
    a = deployment_key("gemma4-26b", host="192.0.2.10", listen_port=8001)
    b = deployment_key("qwen3-8b", host="192.0.2.10", listen_port=8002)
    assert a != b
    assert {a, b} == {a, b, a}


def test_provider_key_is_the_base_and_our_name():
    key = deployment_key("grok-4", api_base="https://openrouter.ai/api/v1")
    assert key == DeploymentKey("https://openrouter.ai/api", "grok-4")


def test_two_names_on_one_provider_model_are_two_keys():
    """grok-4 and grok-4-now may be one upstream model; they are two deployments to us."""
    base = "https://api.x.ai/v1"
    assert deployment_key("grok-4", api_base=base) != deployment_key("grok-4-now", api_base=base)


def test_same_name_from_two_providers_are_two_keys():
    """grok-4 from xAI and from OpenRouter: one name, two bases, two keys."""
    assert deployment_key("grok-4", api_base="https://api.x.ai/v1") != deployment_key(
        "grok-4", api_base="https://openrouter.ai/api/v1"
    )


def test_display_is_host_and_listen_port_for_a_box():
    """What pages and logs print. Never the public port, never a URL to paste."""
    assert deployment_key("gemma4-26b", host="192.0.2.10", listen_port=8001).display == "192.0.2.10:8001"
    assert deployment_key("gemma4-26b", host="2001:db8::1", listen_port=8001).display == "[2001:db8::1]:8001"


def test_display_is_the_base_for_a_provider():
    assert deployment_key("grok-4", api_base="https://api.x.ai/v1").display == "https://api.x.ai"
    assert deployment_key("grok-4", api_base="https://openrouter.ai/api/v1").display == "https://openrouter.ai/api"


def test_key_refuses_half_an_identity():
    with pytest.raises(ValueError):
        deployment_key("", host="192.0.2.10", listen_port=8001)
    with pytest.raises(ValueError):
        deployment_key("gemma4-26b", host="192.0.2.10")
    with pytest.raises(ValueError):
        deployment_key("gemma4-26b", listen_port=8001)
    with pytest.raises(ValueError):
        deployment_key("grok-4", api_base="")
    with pytest.raises(ValueError):
        deployment_key("grok-4", api_base="https://api.x.ai/v1", host="192.0.2.10")

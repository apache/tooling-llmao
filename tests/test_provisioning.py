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

"""provisioning.py: the payload a box installs, rendered from its IntendedDeployments."""

from easydict import EasyDict as edict  # noqa: N813

from llmao.intent import HOSTS, vllm_intent
from llmao.provisioning import config_for_host, server_json
from tests.test_fleet import EXAMPLE, _cfg
from tests.test_intent import _by_name


def test_server_json_is_the_listen_port_and_only_the_declared_requirements():
    cfg = _cfg({})
    model = edict(
        model_name="m",
        litellm_params=edict(model="hosted_vllm/m"),
        model_info=edict(vllm=edict(model="org/m", args=["--x"], gpu_memory_utilization=0.5)),
    )
    body = server_json(vllm_intent(cfg, "10.0.0.1", 8001, "box-m", model, origin=HOSTS, public_port=41234))
    assert body == edict(
        name="box-m",
        model="org/m",
        served_model_name="m",
        host="10.0.0.1",
        port=8001,
        api_key=body.api_key,
        args=["--x"],
        gpu_memory_utilization=0.5,
    )
    assert body.api_key.startswith("sk-vllm-")
    assert "max_model_len" not in body and "vram_gb" not in body


def test_config_for_host_renders_every_row_and_normalizes_the_host():
    cfg = _cfg({"10.0.0.1": [["qwen3-8b", 8003, "box-q"], ["gemma4-26b", 8001]]})
    payload = config_for_host("::ffff:10.0.0.1", models=list(_by_name().values()), cfg=cfg)
    assert payload.host == "10.0.0.1"
    assert [(s.name, s.port) for s in payload.servers] == [("box-q", 8003), ("gemma4-26b", 8001)]
    assert payload.servers[0].model == "Qwen/Qwen3-8B-FP8"
    assert EXAMPLE.is_file()

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

import pytest

from llmao.vllm_api_key import derive_vllm_api_key

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


def test_missing_salt_raises():
    with pytest.raises(ValueError, match="not defined"):
        derive_vllm_api_key(None, "203.0.113.10", 8001)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="missing or CHANGE_ME"):
        derive_vllm_api_key("CHANGE_ME_VLLM_API_SALT", "203.0.113.10", 8001)

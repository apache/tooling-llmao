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
"""Minting a key: what LiteLLM is actually sent.

The seam tests run against MockBackend and never reach this code.
"""

import asyncio

from easydict import EasyDict as edict  # noqa: N813

from llmao.proxy.client import KeyManager
from tests.fake_transport import FakeTransport


class _Teams:
    """Stands in for TeamManager: every project has a team."""

    async def ensure(self, project):
        return edict(team_id=f"team-{project}")


class _Tiers:
    """Stands in for TierManager: the service tier has been cut to 10 a minute."""

    async def key_limits(self, tier):
        return {"rpm_limit": 10 if tier == "service" else 120, "tpm_limit": 60000}


def _keys():
    def litellm(call):
        # key/generate answers with the key it made
        return {"key": "sk-secret", "token": "tok1", **call.json}

    return KeyManager(FakeTransport(litellm), _Teams(), _Tiers())


def _sent(keys):
    return keys._transport.calls[-1]


def test_a_key_is_minted_with_its_tiers_limits():
    keys = _keys()
    created = asyncio.run(keys.create(project="airflow", purpose="ci", user="jdoe", tier="service"))
    sent = _sent(keys)
    assert (sent.method, sent.path) == ("POST", "key/generate")
    assert sent.json.rpm_limit == 10, "the tier's limit as it stands now, not as config had it"
    assert sent.json.tpm_limit == 60000
    assert sent.json.team_id == "team-airflow"
    assert sent.json.user_id == "jdoe"
    assert created.secret == "sk-secret"
    assert created.info.project == "airflow"


def test_the_tier_asked_for_is_the_tier_recorded():
    """Metadata naming one tier while the limits came from another is how an audit goes wrong."""
    keys = _keys()
    asyncio.run(keys.create(project="airflow", purpose="ci", user="jdoe", tier="project", metadata={"tier": "service"}))
    sent = _sent(keys)
    assert sent.json.metadata.tier == "project"
    assert sent.json.rpm_limit == 120

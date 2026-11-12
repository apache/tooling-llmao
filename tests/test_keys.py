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


def _paged(total_keys: int, *, send_total_pages: bool = True):
    """A LiteLLM that holds `total_keys` keys and answers key/list in pages."""
    fake = None

    def litellm(call):
        if call.path != "key/list":
            return {"key": "sk-secret", "token": "tok1", **(call.json or {})}
        size = int(call.params["size"])
        page = int(call.params["page"])
        start = (page - 1) * size
        rows = [
            {"token": f"tok{i}", "key_alias": f"k{i}", "user_id": "jdoe", "metadata": {"project": "tooling"}}
            for i in range(start, min(start + size, total_keys))
        ]
        body = {"keys": rows}
        if send_total_pages:
            body["total_pages"] = max(1, -(-total_keys // size))
        return body

    fake = FakeTransport(litellm)
    return KeyManager(fake, _Teams(), _Tiers()), fake


def test_find_walks_past_the_first_page():
    """revoke_key looks a key up through find, so a key on page 2 must be found.

    With one page only, a deployment past `size` keys had keys it could not
    revoke -- reported as not existing, which is worse than failing.
    """
    keys, fake = _paged(250)
    found = asyncio.run(keys.find(size=100))
    assert [k.token_id for k in found][-1] == "tok249"
    assert len(found) == 250
    assert [c.params["page"] for c in fake.calls] == [1, 2, 3]


def test_find_stops_on_a_short_page_without_total_pages():
    keys, fake = _paged(150, send_total_pages=False)
    assert len(asyncio.run(keys.find(size=100))) == 150
    assert [c.params["page"] for c in fake.calls] == [1, 2]


def test_find_with_a_limit_asks_for_one_page_only():
    """The 'does this user have any key' probe must stay one cheap call."""
    keys, fake = _paged(250)
    assert len(asyncio.run(keys.find(size=1, limit=1))) == 1
    assert len(fake.calls) == 1


def test_find_returns_nothing_when_there_are_no_keys():
    keys, fake = _paged(0)
    assert asyncio.run(keys.find()) == []
    assert len(fake.calls) == 1

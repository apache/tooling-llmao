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

"""Offline tests: mock backend + seam authz.

Authenticated HTTP endpoints require a real asfquart session (OAuth). Those
paths are not automated while the stack is in flux; expand later if needed.
Model inventory tests live in test_models.py.
Run with: pytest -q
"""

import asyncio
import time

import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.seam import AuthzError, Identity, Seam
from tests.mock_backend import MockBackend


def _cfg():
    return edict(
        {
            "litellm": {
                "base_url": "http://127.0.0.1:4000",
                "master_key": "sk-test",
                "request_timeout_s": 30,
            },
            "budgets": {
                "default_team_budget_usd": 100.0,
                "duration": "30d",
            },
            "site_admins": ["root"],
            "models_path": "models.yaml",
        }
    )


def _seam():
    cfg = _cfg()
    backend = MockBackend(cfg)
    return Seam(cfg, backend), cfg, backend


def _seed_usage(backend: MockBackend, project: str, cost: float = 0.01) -> None:
    backend._data.setdefault("usage", []).append(
        {
            "ts": time.time(),
            "project": project,
            "model": "selfhost/gemma4-26b",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cost_usd": cost,
        }
    )
    teams = backend._data.setdefault("teams", {})
    if project in teams:
        teams[project]["spend"] = round(teams[project].get("spend", 0.0) + cost, 6)


def test_cfg_dotted_access():
    cfg = edict(
        {
            "litellm": {"base_url": "http://llm:4000", "master_key": "sk-x"},
            "budgets": {"default_team_budget_usd": 50, "duration": "7d"},
            "site_admins": ["alice"],
        }
    )
    assert cfg.litellm.base_url == "http://llm:4000"
    assert cfg.budgets.default_team_budget_usd == 50
    assert cfg.site_admins == ["alice"]


def test_create_key_provisions_project_team():
    """create_key ensures project team exists (mock: team_id == project)."""

    async def run():
        seam, cfg, _ = _seam()
        ident = Identity(uid="jdoe", projects=["airflow"], committees=[])
        assert await seam.team_status(ident, "airflow") is None
        created = await seam.create_personal_key(ident, "airflow", "cli")
        assert created.info.project == "airflow"
        assert created.info.team_id == "airflow"
        info = await seam.team_status(ident, "airflow")
        assert info is not None
        assert info.team_id == "airflow"
        assert info.max_budget == float(cfg.budgets.default_team_budget_usd)

    asyncio.run(run())


def test_personal_key_create_list_revoke_mock():
    async def run():
        seam, _, _ = _seam()
        ident = Identity(uid="jdoe", projects=["airflow"], committees=[])
        created = await seam.create_personal_key(ident, "airflow", "laptop-cli")
        assert created.secret.startswith("sk-")
        assert created.info.purpose == "laptop-cli"
        assert created.info.project == "airflow"
        assert created.info.user == "jdoe"
        assert not created.info.is_automation
        assert created.info.created_by is None
        keys = await seam.list_my_keys(ident)
        assert len(keys) == 1
        await seam.revoke_key(ident, keys[0].token_id)
        assert await seam.list_my_keys(ident) == []

    asyncio.run(run())


def test_automation_key_records_created_by():
    async def run():
        seam, _, _ = _seam()
        admin = Identity(uid="chair", projects=[], committees=["airflow"])
        created = await seam.create_automation_key(admin, "airflow", "INFRA-123-ci")
        assert created.info.is_automation
        assert created.info.user is None
        assert created.info.created_by == "chair"
        assert created.info.project == "airflow"
        assert created.info.purpose == "INFRA-123-ci"
        listed = await seam.list_automation_keys(admin, "airflow")
        assert len(listed) == 1
        assert listed[0].created_by == "chair"

    asyncio.run(run())


def test_team_status_requires_member():
    async def run():
        seam, _, _ = _seam()
        ident = Identity(uid="jdoe", projects=["airflow"], committees=[])
        with pytest.raises(AuthzError):
            await seam.team_status(ident, "kafka")
        # Member of airflow may query; none provisioned yet.
        assert await seam.team_status(ident, "airflow") is None

    asyncio.run(run())


def test_activity_requires_pmc_admin():
    async def run():
        seam, _, backend = _seam()
        member = Identity(uid="jdoe", projects=["airflow"], committees=[])
        admin = Identity(uid="chair", projects=[], committees=["airflow"])
        backend._touch_team("airflow")
        _seed_usage(backend, "airflow")
        with pytest.raises(AuthzError):
            await seam.project_activity(member, "airflow")
        rows = await seam.project_activity(admin, "airflow")
        assert len(rows) == 1

    asyncio.run(run())


def test_site_admin_sees_any_project():
    async def run():
        seam, _, backend = _seam()
        root = Identity(uid="root", projects=[], committees=[], is_site_admin=True)
        backend._touch_team("airflow")
        _seed_usage(backend, "airflow")
        rows = await seam.project_activity(root, "airflow")
        assert len(rows) == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Which tier a key is minted on
#
# The tier decides a key's rate limits, and those are the only control there
# is on self-hosted capacity. Minting used to default to the project tier,
# so a path that did not say got project limits whatever it was for.
# ---------------------------------------------------------------------------


def _minted_tier(backend):
    return backend._data["keys"][-1]["metadata"]["tier"]


def test_a_persons_project_key_is_on_the_project_tier():
    async def run():
        seam, _, backend = _seam()
        await seam.create_personal_key(Identity(uid="jdoe", projects=["airflow"], committees=[]), "airflow", "cli")
        assert _minted_tier(backend) == "project"

    asyncio.run(run())


def test_an_automation_key_is_on_the_service_tier():
    """No person at the keyboard is what the service tier is for."""

    async def run():
        seam, _, backend = _seam()
        await seam.create_automation_key(Identity(uid="chair", projects=[], committees=["airflow"]), "airflow", "ci")
        assert _minted_tier(backend) == "service"

    asyncio.run(run())


def test_a_key_request_cannot_name_a_tier_that_does_not_exist():
    """The tier comes from a form post and ends up deciding a key's limits."""

    async def run():
        seam, _, _ = _seam()
        ident = Identity(uid="jdoe", projects=[], committees=[])
        with pytest.raises(AuthzError, match="no platinum tier"):
            await seam.request_key(ident, purpose="ci", tier="platinum", reason="because")

    asyncio.run(run())


def test_an_admin_cannot_change_a_tier_that_does_not_exist():
    async def run():
        seam, _, _ = _seam()
        root = Identity(uid="root", projects=[], committees=[], is_site_admin=True)
        with pytest.raises(AuthzError, match="no platinum tier"):
            await seam.set_tier(root, "platinum", rpm_limit=1)

    asyncio.run(run())

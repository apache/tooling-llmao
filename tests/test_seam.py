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

"""The seam, against the in-memory stand-in for LiteLLM (tests/mock_backend.py).

Who may do what, and how the seam puts together what the managers give it:
keys, the project list and a project's overview. Nothing here reaches
LiteLLM or needs a signed-in session.
"""

import asyncio
import time

import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.proxy.client import (
    GRANTOR_FREE_TIER,
    PRETEND_PROJECTS,
    PROJECT_FREE_TIER,
    PROJECT_SERVICE,
    AccessRequestManager,
    BackendUnavailableError,
    LogManager,
    MemoryStore,
    SkewManager,
    pretend_project,
    resolve_budget_duration,
)
from llmao.seam import AuthzError, Identity, Seam
from tests.fake_transport import fake_memory
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
# The project list, and one project's overview
# ---------------------------------------------------------------------------


def test_resolve_budget_duration_prefers_raw_then_cfg():
    cfg = _cfg()
    assert resolve_budget_duration("7d", cfg) == "7d"
    assert resolve_budget_duration(None, cfg) == "30d"
    assert resolve_budget_duration("  ", cfg) == "30d"
    bad = edict({"budgets": {}})
    with pytest.raises(BackendUnavailableError):
        resolve_budget_duration(None, bad)


def test_list_projects_empty_membership():
    async def run():
        seam, _, _ = _seam()
        ident = Identity(uid="jdoe", projects=[], committees=[])
        assert await seam.list_projects_for(ident) == []

    asyncio.run(run())


def test_list_projects_ensures_team_and_budget():
    async def run():
        seam, cfg, backend = _seam()
        ident = Identity(uid="jdoe", projects=["airflow"], committees=[])
        assert await backend.team_info("airflow") is None
        rows = await seam.list_projects_for(ident)
        assert len(rows) == 1
        r = rows[0]
        assert r.project == "airflow"
        assert r.is_steward is False
        assert r.max_budget == float(cfg.budgets.default_team_budget_usd)
        assert r.spend == 0.0
        assert r.remaining == r.max_budget
        assert r.pct_used == 0.0
        assert r.budget_duration == "30d"
        assert r.grantor == GRANTOR_FREE_TIER
        info = await backend.team_info("airflow")
        assert info is not None
        assert info.budget_duration == "30d"

    asyncio.run(run())


def test_list_projects_steward_flag():
    async def run():
        seam, _, _ = _seam()
        ident = Identity(
            uid="chair",
            projects=["kafka"],
            committees=["airflow"],
        )
        rows = await seam.list_projects_for(ident)
        by_name = {r.project: r for r in rows}
        assert by_name["airflow"].is_steward is True
        assert by_name["kafka"].is_steward is False
        assert sorted(by_name) == ["airflow", "kafka"]

    asyncio.run(run())


def test_overview_authz_outsider():
    async def run():
        seam, _, _ = _seam()
        ident = Identity(uid="jdoe", projects=["airflow"], committees=[])
        with pytest.raises(AuthzError):
            await seam.project_overview(ident, "kafka")

    asyncio.run(run())


def test_overview_ensures_team():
    async def run():
        seam, cfg, backend = _seam()
        ident = Identity(uid="jdoe", projects=["airflow"], committees=[])
        ov = await seam.project_overview(ident, "airflow")
        assert ov.project == "airflow"
        assert ov.max_budget == float(cfg.budgets.default_team_budget_usd)
        assert ov.budget_duration == "30d"
        assert ov.grantor == GRANTOR_FREE_TIER
        assert ov.people_spend == 0.0
        assert ov.automation_spend == 0.0
        assert ov.by_person == []
        assert ov.automation_key_count == 0
        assert await backend.team_info("airflow") is not None

    asyncio.run(run())


def test_overview_people_automation_and_by_person():
    async def run():
        seam, _, backend = _seam()
        admin = Identity(uid="chair", projects=[], committees=["airflow"])
        alice = Identity(uid="alice", projects=["airflow"], committees=[])
        bob = Identity(uid="bob", projects=["airflow"], committees=[])

        k_alice = await seam.create_personal_key(alice, "airflow", "cli")
        k_bob1 = await seam.create_personal_key(bob, "airflow", "a")
        k_bob2 = await seam.create_personal_key(bob, "airflow", "b")
        k_auto = await seam.create_automation_key(admin, "airflow", "ci")

        backend.set_key_spend(k_alice.info.token_id, 10.0)
        backend.set_key_spend(k_bob1.info.token_id, 3.0)
        backend.set_key_spend(k_bob2.info.token_id, 4.0)
        backend.set_key_spend(k_auto.info.token_id, 2.5)
        backend.set_team_spend("airflow", 50.0)

        # Member (non-steward) sees transparency.
        ov = await seam.project_overview(alice, "airflow")
        assert ov.is_steward is False
        assert ov.spend == 50.0
        assert ov.people_spend == 17.0
        assert ov.automation_spend == 2.5
        assert [p.uid for p in ov.by_person] == ["alice", "bob"]
        assert ov.by_person[0].spend == 10.0
        assert ov.by_person[0].key_count == 1
        assert ov.by_person[1].spend == 7.0
        assert ov.by_person[1].key_count == 2
        assert ov.automation_key_count == 1
        assert ov.automation_keys[0].created_by == "chair"
        assert ov.automation_keys[0].spend == 2.5
        assert ov.grantor == GRANTOR_FREE_TIER

    asyncio.run(run())


def test_site_admin_overview_without_membership():
    async def run():
        seam, _, _ = _seam()
        root = Identity(uid="root", projects=[], committees=[], is_site_admin=True)
        # Not on list (all_projects empty)
        assert await seam.list_projects_for(root) == []
        ov = await seam.project_overview(root, "airflow")
        assert ov.project == "airflow"
        assert ov.is_steward is True  # site admin
        assert ov.grantor == GRANTOR_FREE_TIER

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


# ---------------------------------------------------------------------------
# A key for no project
#
# It still has to be in a LiteLLM team. There are two pretend projects for
# that, one per kind of projectless key, so their usage can be told apart
# and their models restricted in one place.
# ---------------------------------------------------------------------------


def _approved_key(tier):
    """Ask for a projectless key on `tier`, have it approved, and create it."""

    async def run():
        seam, _, backend = _seam()
        # the mock has no requests of its own; the real manager will do
        backend.requests = AccessRequestManager(MemoryStore(fake_memory()[0]))
        jdoe = Identity(uid="jdoe", projects=[], committees=[])
        root = Identity(uid="root", projects=[], committees=[], is_site_admin=True)
        req = await seam.request_key(jdoe, purpose="ci", tier=tier, reason="because")
        await seam.decide_request(root, req.id, approve=True)
        return (await seam.create_approved_key(jdoe, req.id)).info

    return asyncio.run(run())


def test_a_projectless_free_tier_key_goes_in_the_free_tier_project():
    assert _approved_key("free_tier").project == PROJECT_FREE_TIER


def test_a_projectless_service_key_goes_in_the_service_project():
    assert _approved_key("service").project == PROJECT_SERVICE


def test_a_pretend_project_cannot_be_an_ldap_one():
    """LDAP names have no colon, which is what keeps these from colliding."""
    assert all(":" in name for name in PRETEND_PROJECTS)
    assert pretend_project("free_tier") == PROJECT_FREE_TIER
    assert pretend_project("service") == PROJECT_SERVICE


# ---------------------------------------------------------------------------
# Acknowledging
#
# The key comes from a form post and names a row to delete.
# ---------------------------------------------------------------------------


def _with_log():
    seam, _, backend = _seam()
    transport, store = fake_memory()
    backend.log = LogManager(MemoryStore(transport))
    backend.skew = SkewManager(MemoryStore(transport))
    return seam, backend, store


ALICE = Identity(uid="alice", projects=[], committees=[])
BOB = Identity(uid="bob", projects=[], committees=[])
ROOT = Identity(uid="root", projects=[], committees=[], is_site_admin=True)


def test_a_person_acknowledges_what_was_addressed_to_them():
    async def run():
        seam, backend, store = _with_log()
        await backend.log.note("a site admin revoked your key ci", user="alice")
        (entry,) = await seam.my_notices(ALICE)
        await seam.acknowledge(ALICE, entry.key)
        assert store == {}

    asyncio.run(run())


def test_nobody_acknowledges_what_was_addressed_to_someone_else():
    async def run():
        seam, backend, store = _with_log()
        await backend.log.note("yours", user="alice")
        await backend.log.note("for the admins")
        mine = next(k for k in store if ":user:alice:" in k)
        admins = next(k for k in store if ":admin:" in k)
        for who, key in ((BOB, mine), (ALICE, admins), (ROOT, mine)):
            with pytest.raises(AuthzError):
                await seam.acknowledge(who, key)
        assert len(store) == 2
        await seam.acknowledge(ROOT, admins)
        assert list(store) == [mine]

    asyncio.run(run())


def test_skew_cannot_be_acknowledged_away():
    """It goes when the work is done, and by no other route."""

    async def run():
        seam, backend, store = _with_log()
        await backend.skew.reconcile("key", {"t1": {"what": "limits differ"}})
        with pytest.raises(AuthzError):
            await seam.acknowledge(ROOT, "llmao:skew:key:t1")
        assert list(store) == ["llmao:skew:key:t1"]

    asyncio.run(run())


def test_only_a_site_admin_reads_the_admins_notices():
    async def run():
        seam, _, _ = _with_log()
        with pytest.raises(AuthzError):
            await seam.admin_notices(ALICE)
        assert await seam.admin_notices(ROOT) == ([], [])

    asyncio.run(run())

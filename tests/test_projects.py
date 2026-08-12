"""P0.1: project list + overview seam (mock backend)."""
import asyncio

import pytest
from easydict import EasyDict as edict

from llmao.litellm_client import BackendUnavailable, resolve_budget_duration
from llmao.seam import (
    BUDGET_TYPE_UNKNOWN,
    AuthzError,
    Identity,
    Seam,
)
from tests.mock_backend import MockBackend


def _cfg():
    return edict({
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
        "models_path": "model_list.yaml.example",
    })


def _seam():
    cfg = _cfg()
    backend = MockBackend(cfg)
    return Seam(cfg, backend), cfg, backend


def test_resolve_budget_duration_prefers_raw_then_cfg():
    cfg = _cfg()
    assert resolve_budget_duration("7d", cfg) == "7d"
    assert resolve_budget_duration(None, cfg) == "30d"
    assert resolve_budget_duration("  ", cfg) == "30d"
    bad = edict({"budgets": {}})
    with pytest.raises(BackendUnavailable):
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
        assert r.budget_type == BUDGET_TYPE_UNKNOWN
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
        assert ov.budget_type == BUDGET_TYPE_UNKNOWN
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
        assert ov.budget_type == BUDGET_TYPE_UNKNOWN
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
        assert ov.budget_type == BUDGET_TYPE_UNKNOWN
    asyncio.run(run())

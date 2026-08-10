"""Offline tests: mock backend + seam authz.

Authenticated HTTP endpoints require a real asfquart session (OAuth). Those
paths are not automated while the stack is in flux; expand later if needed.
Model inventory tests live in test_models.py.
Run with: pytest -q
"""
import asyncio
import os
import tempfile
import time

import pytest
from easydict import EasyDict as edict

from llmao.store import StateStore
from llmao.seam import Seam, Identity, AuthzError
from tests.mock_backend import MockBackend


def _cfg(tmp):
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
        "state_path": os.path.join(tmp, "state.json"),
        "models_path": "model_list.yaml.example",
    })


def _seam(tmp):
    cfg = _cfg(tmp)
    store = StateStore(cfg.state_path)
    return Seam(cfg, MockBackend(cfg, store)), cfg, store


def _seed_usage(store: StateStore, project: str, cost: float = 0.01) -> None:
    def _mut(data):
        data.setdefault("usage", []).append({
            "ts": time.time(),
            "project": project,
            "model": "selfhost/gemma4-26b",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cost_usd": cost,
        })
        teams = data.setdefault("teams", {})
        if project in teams:
            teams[project]["spend"] = round(teams[project].get("spend", 0.0) + cost, 6)
    store.update(_mut)


def test_cfg_dotted_access():
    cfg = edict({
        "litellm": {"base_url": "http://llm:4000", "master_key": "sk-x"},
        "budgets": {"default_team_budget_usd": 50, "duration": "7d"},
        "site_admins": ["alice"],
        "state_path": "/tmp/s.json",
    })
    assert cfg.litellm.base_url == "http://llm:4000"
    assert cfg.budgets.default_team_budget_usd == 50
    assert cfg.site_admins == ["alice"]


def test_ensure_team_provisions_budget():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            seam, cfg, _ = _seam(tmp)
            info = await seam.ensure_project_team("airflow")
            assert info.team_id
            # Team ensure does not mint a product PAT (key stays empty).
            assert info.max_budget == float(cfg.budgets.default_team_budget_usd)
            again = await seam.ensure_project_team("airflow")
            assert again.team_id == info.team_id
    asyncio.run(run())


def test_personal_key_create_list_revoke_mock():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            seam, _, _ = _seam(tmp)
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
        with tempfile.TemporaryDirectory() as tmp:
            seam, _, _ = _seam(tmp)
            admin = Identity(uid="chair", projects=[], committees=["airflow"])
            created = await seam.create_automation_key(
                admin, "airflow", "INFRA-123-ci"
            )
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
        with tempfile.TemporaryDirectory() as tmp:
            seam, _, _ = _seam(tmp)
            ident = Identity(uid="jdoe", projects=["airflow"], committees=[])
            with pytest.raises(AuthzError):
                await seam.team_status(ident, "kafka")
            # Member of airflow may query; none provisioned yet.
            assert await seam.team_status(ident, "airflow") is None
    asyncio.run(run())


def test_activity_requires_pmc_admin():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            seam, _, store = _seam(tmp)
            member = Identity(uid="jdoe", projects=["airflow"], committees=[])
            admin = Identity(uid="chair", projects=[], committees=["airflow"])
            await seam.ensure_project_team("airflow")
            _seed_usage(store, "airflow")
            with pytest.raises(AuthzError):
                await seam.project_activity(member, "airflow")
            rows = await seam.project_activity(admin, "airflow")
            assert len(rows) == 1
    asyncio.run(run())


def test_site_admin_sees_any_project():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            seam, _, store = _seam(tmp)
            root = Identity(uid="root", projects=[], committees=[], is_site_admin=True)
            await seam.ensure_project_team("airflow")
            _seed_usage(store, "airflow")
            rows = await seam.project_activity(root, "airflow")
            assert len(rows) == 1
    asyncio.run(run())

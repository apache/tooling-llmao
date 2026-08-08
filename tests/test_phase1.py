"""Offline tests: catalog, mock team provision, seam authz.

Authenticated HTTP endpoints require a real asfquart session (OAuth). Those
paths are not automated while the stack is in flux; expand later if needed.
Run with: pytest -q
"""
import asyncio
import os
import tempfile
import time

import pytest

from llmao.config import Settings
from llmao.store import StateStore
from llmao.litellm_client import MockBackend
from llmao.seam import Seam, Identity, AuthzError
from llmao import catalog


def _settings(tmp):
    return Settings(
        litellm_mode="mock",
        state_path=os.path.join(tmp, "state.json"),
        default_team_budget_usd=100.0,
        site_admins=["root"],
    )


def _seam(tmp):
    s = _settings(tmp)
    store = StateStore(s.state_path)
    return Seam(s, MockBackend(s, store)), s, store


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


def test_settings_from_cfg():
    s = Settings.from_cfg({
        "litellm": {"mode": "proxy", "base_url": "http://llm:4000", "master_key": "sk-x"},
        "budgets": {"default_team_budget_usd": 50, "duration": "7d"},
        "site_admins": ["alice"],
        "state_path": "/tmp/s.json",
    })
    assert s.litellm_mode == "proxy"
    assert not s.is_mock_llm
    assert s.litellm_base_url == "http://llm:4000"
    assert s.default_team_budget_usd == 50
    assert s.budget_duration == "7d"
    assert s.site_admins == ["alice"]


def test_catalog_has_governance_metadata():
    for m in catalog.all_models():
        assert m["license"]
        assert m["openness"] in ("open-weight", "open-source", "proprietary")
        assert m["provenance_record"] in ("present", "absent")


def test_ensure_team_provisions_budget():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            seam, s, _ = _seam(tmp)
            info = await seam.ensure_project_team("airflow")
            assert info.team_id
            assert info.key.startswith("sk-")
            assert info.max_budget == s.default_team_budget_usd
            assert info.spend == 0.0
            again = await seam.ensure_project_team("airflow")
            assert again.team_id == info.team_id
    asyncio.run(run())


def test_require_member_refuses_outsider():
    with tempfile.TemporaryDirectory() as tmp:
        seam, _, _ = _seam(tmp)
        ident = Identity(uid="jdoe", projects=["airflow"], committees=[])
        with pytest.raises(AuthzError):
            seam.require_member(ident, "kafka")
        seam.require_member(ident, "airflow")


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

"""Control-plane tests: seam, authz, team provision, budget/usage HTTP.

Run with: pytest -q
These exercise the mock backend so no litellm proxy or ASF auth is needed.
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
        auth_mode="dev",
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


def test_catalog_has_governance_metadata():
    # catalog.py is still used by make config; keep a smoke assertion.
    for m in catalog.all_models():
        assert m["license"]
        assert m["openness"] in ("open-weight", "open-source", "proprietary")
        assert m["provenance_record"] in ("present", "absent")


def test_ensure_team_provisions_budget():
    with tempfile.TemporaryDirectory() as tmp:
        seam, s, _ = _seam(tmp)
        info = seam.ensure_project_team("airflow")
        assert info.team_id
        assert info.key.startswith("sk-")
        assert info.max_budget == s.default_team_budget_usd
        assert info.spend == 0.0
        # Idempotent
        again = seam.ensure_project_team("airflow")
        assert again.team_id == info.team_id


def test_require_member_refuses_outsider():
    with tempfile.TemporaryDirectory() as tmp:
        seam, _, _ = _seam(tmp)
        ident = Identity(uid="jdoe", projects=["airflow"], committees=[])
        with pytest.raises(AuthzError):
            seam.require_member(ident, "kafka")
        seam.require_member(ident, "airflow")  # no raise


def test_activity_requires_pmc_admin():
    with tempfile.TemporaryDirectory() as tmp:
        seam, _, store = _seam(tmp)
        member = Identity(uid="jdoe", projects=["airflow"], committees=[])
        admin = Identity(uid="chair", projects=[], committees=["airflow"])
        seam.ensure_project_team("airflow")
        _seed_usage(store, "airflow")
        with pytest.raises(AuthzError):
            seam.project_activity(member, "airflow")
        rows = seam.project_activity(admin, "airflow")
        assert len(rows) == 1


def test_site_admin_sees_any_project():
    with tempfile.TemporaryDirectory() as tmp:
        seam, _, store = _seam(tmp)
        root = Identity(uid="root", projects=[], committees=[], is_site_admin=True)
        seam.ensure_project_team("airflow")
        _seed_usage(store, "airflow")
        rows = seam.project_activity(root, "airflow")
        assert len(rows) == 1


def test_http_budget_and_authz():
    with tempfile.TemporaryDirectory() as tmp:
        s = _settings(tmp)
        from llmao.app import create_app
        app = create_app(s)

        async def run():
            client = app.test_client()
            r = await client.get("/healthz")
            assert r.status_code == 200

            r = await client.get("/v1/projects/airflow/budget")
            assert r.status_code == 401

            await client.post(
                "/auth/dev/login",
                form={"uid": "jdoe", "projects": "airflow", "committees": ""},
            )
            r = await client.get("/v1/projects/airflow/budget")
            assert r.status_code == 200
            body = await r.get_json()
            assert body["provisioned"] is False

            # Provision via seam (no public mint API yet in Phase A).
            seam = app.config["LLMAO_SEAM"]
            seam.ensure_project_team("airflow")

            r = await client.get("/v1/projects/airflow/budget")
            body = await r.get_json()
            assert body["provisioned"] is True
            assert body["max_budget_usd"] == s.default_team_budget_usd

            r = await client.get("/v1/projects/kafka/budget")
            assert r.status_code == 403

            r = await client.get("/v1/projects/airflow/usage")
            assert r.status_code == 403  # member, not PMC admin

        asyncio.run(run())


def test_http_usage_for_pmc_admin():
    with tempfile.TemporaryDirectory() as tmp:
        s = _settings(tmp)
        from llmao.app import create_app
        app = create_app(s)
        seam = app.config["LLMAO_SEAM"]
        store = StateStore(s.state_path)
        seam.ensure_project_team("airflow")
        _seed_usage(store, "airflow", cost=0.02)

        async def run():
            client = app.test_client()
            await client.post(
                "/auth/dev/login",
                form={"uid": "chair", "projects": "", "committees": "airflow"},
            )
            r = await client.get("/v1/projects/airflow/usage")
            assert r.status_code == 200
            body = await r.get_json()
            assert body["count"] == 1
            assert body["total_cost_usd"] == 0.02

        asyncio.run(run())

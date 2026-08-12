"""In-process LiteLLM stand-in for the test suite only.

Speaks **project** (LDAP name) like the product Backend. Uses the project
string as the opaque team identity — no separate team_id map.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from llmao.litellm_client import (
    CreatedKey,
    KeyInfo,
    TeamInfo,
    _normalize_key_obj,
    resolve_budget_duration,
)


class MockBackend:
    def __init__(self, cfg: Any):
        self._cfg = cfg
        self._data: Dict[str, Any] = {
            "teams": {},
            "keys": [],
            "usage": [],
        }

    async def team_info(self, project: str) -> Optional[TeamInfo]:
        t = self._data.get("teams", {}).get(project)
        if not t:
            return None
        return TeamInfo(
            t.get("team_id", project),
            float(t.get("max_budget", 0.0)),
            float(t.get("spend", 0.0)),
            budget_duration=resolve_budget_duration(
                t.get("budget_duration") or t.get("duration"),
                self._cfg,
            ),
        )

    def _touch_team(self, project: str) -> None:
        teams = self._data.setdefault("teams", {})
        if project not in teams:
            teams[project] = {
                "team_id": project,  # project is the opaque id in the mock
                "max_budget": float(self._cfg.budgets.default_team_budget_usd),
                "spend": 0.0,
                "duration": str(self._cfg.budgets.duration),
                "budget_duration": str(self._cfg.budgets.duration),
                "created_at": time.time(),
            }

    async def ensure_team(self, project: str) -> TeamInfo:
        project = (project or "").strip()
        if not project:
            raise ValueError("project is required")
        self._touch_team(project)
        info = await self.team_info(project)
        assert info is not None
        return info

    async def list_keys(
        self,
        *,
        user: Optional[str] = None,
        project: Optional[str] = None,
        size: int = 100,
    ) -> List[KeyInfo]:
        out = []
        for k in self._data.get("keys", []):
            if user is not None and k.get("user_id") != user:
                continue
            if project is not None and k.get("team_id") != project:
                continue
            out.append(_normalize_key_obj(k))
            if len(out) >= size:
                break
        return out

    async def create_key(
        self,
        *,
        project: str,
        purpose: str,
        user: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> CreatedKey:
        project = (project or "").strip()
        purpose = (purpose or "").strip()
        await self.ensure_team(project)
        secret = f"sk-mock-{uuid.uuid4().hex}"
        token_id = f"tok-{uuid.uuid4().hex[:16]}"
        meta = dict(metadata or {})
        meta["project"] = project
        if purpose:
            meta["purpose"] = purpose
        row = {
            "token": token_id,
            "team_id": project,
            "user_id": user,
            "spend": 0.0,
            "max_budget": None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "last_used": None,
            "metadata": meta,
        }
        self._data.setdefault("keys", []).append(row)
        return CreatedKey(secret=secret, info=_normalize_key_obj(row))

    def set_key_spend(self, token_id: str, spend: float) -> None:
        for k in self._data.get("keys", []):
            if k.get("token") == token_id:
                k["spend"] = float(spend)
                return
        raise KeyError(token_id)

    def set_team_spend(self, project: str, spend: float) -> None:
        self._touch_team(project)
        self._data["teams"][project]["spend"] = float(spend)

    async def delete_key(self, token_id: str) -> None:
        self._data["keys"] = [
            k for k in self._data.get("keys", []) if k.get("token") != token_id
        ]

    async def usage(self, project: Optional[str]) -> List[Dict]:
        rows = self._data.get("usage", [])
        if project is None:
            return list(rows)
        return [r for r in rows if r.get("project") == project]

    async def aclose(self) -> None:
        return None

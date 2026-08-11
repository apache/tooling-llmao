"""In-process LiteLLM stand-in for the test suite only.

The running app always uses LiteLLMBackend against a real proxy. Tests inject
this mock so seam authz and PAT flows can run offline. State lives in a plain
in-memory dict — no StateStore / disk.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from llmao.litellm_client import CreatedKey, KeyInfo, TeamInfo, _normalize_key_obj


class MockBackend:
    def __init__(self, cfg: Any):
        self._cfg = cfg
        self._data: Dict[str, Any] = {
            "teams": {},
            "keys": [],
            "usage": [],
        }

    async def ensure_team(self, project: str, budget_usd: float, duration: str) -> TeamInfo:
        teams = self._data.setdefault("teams", {})
        if project not in teams:
            teams[project] = {
                "team_id": f"team-{uuid.uuid4().hex[:12]}",
                "max_budget": budget_usd,
                "spend": 0.0,
                "duration": duration,
                "created_at": time.time(),
            }
        t = teams[project]
        return TeamInfo(t["team_id"], t.get("max_budget", 0.0), t.get("spend", 0.0))

    async def team_info(self, project: str) -> Optional[TeamInfo]:
        t = self._data.get("teams", {}).get(project)
        if not t:
            return None
        return TeamInfo(t["team_id"], t.get("max_budget", 0.0), t.get("spend", 0.0))

    async def list_keys(
        self,
        *,
        user: Optional[str] = None,
        team_id: Optional[str] = None,
        size: int = 100,
    ) -> List[KeyInfo]:
        out = []
        for k in self._data.get("keys", []):
            if user is not None and k.get("user_id") != user:
                continue
            if team_id is not None and k.get("team_id") != team_id:
                continue
            out.append(_normalize_key_obj(k))
            if len(out) >= size:
                break
        return out

    async def create_key(
        self,
        *,
        team_id: str,
        purpose: str,
        user: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> CreatedKey:
        secret = f"sk-mock-{uuid.uuid4().hex}"
        token_id = f"tok-{uuid.uuid4().hex[:16]}"
        row = {
            "token": token_id,
            "key_alias": purpose,
            "team_id": team_id,
            "user_id": user,
            "spend": 0.0,
            "max_budget": None,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "last_used": None,
            "metadata": metadata or {},
        }
        self._data.setdefault("keys", []).append(row)
        return CreatedKey(secret=secret, info=_normalize_key_obj(row))

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

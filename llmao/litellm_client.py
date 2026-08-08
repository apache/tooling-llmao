"""LiteLLM admin backend (async).

Two implementations behind one interface:

* ``ProxyBackend`` talks to a real LiteLLM proxy over **httpx** (async).
  Admin endpoints: /team/new, /key/generate, /team/info. Completions are not
  proxied here — clients call LiteLLM with virtual keys.

* ``MockBackend`` fakes team provision and usage in-process (laptop / CI).

``cfg`` is the asfquart EasyDict (``APP.cfg``): use dotted access
(``cfg.litellm.mode``, etc.). Project names are LDAP/session names.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

import httpx

from .store import StateStore


class BudgetExceeded(Exception):
    """Raised when a team is over budget (mirrors litellm proxy's 4xx)."""


class BackendUnavailable(Exception):
    """Raised when the litellm admin API times out or can't be reached."""


@dataclass
class TeamInfo:
    team_id: str
    key: str
    max_budget: float
    spend: float


class Backend(Protocol):
    async def ensure_team(self, project: str, budget_usd: float, duration: str) -> TeamInfo: ...
    async def team_info(self, project: str) -> Optional[TeamInfo]: ...
    async def usage(self, project: Optional[str]) -> List[Dict]: ...
    async def aclose(self) -> None: ...


class MockBackend:
    def __init__(self, cfg: Any, store: StateStore):
        self._cfg = cfg
        self._store = store

    async def ensure_team(self, project: str, budget_usd: float, duration: str) -> TeamInfo:
        def _mut(data):
            teams = data.setdefault("teams", {})
            if project not in teams:
                teams[project] = {
                    "team_id": f"team-{uuid.uuid4().hex[:12]}",
                    "key": f"sk-team-{uuid.uuid4().hex[:24]}",
                    "max_budget": budget_usd,
                    "spend": 0.0,
                    "duration": duration,
                    "created_at": time.time(),
                }
            t = teams[project]
            return TeamInfo(t["team_id"], t["key"], t["max_budget"], t["spend"])
        return self._store.update(_mut)

    async def team_info(self, project: str) -> Optional[TeamInfo]:
        teams = self._store.snapshot().get("teams", {})
        t = teams.get(project)
        if not t:
            return None
        return TeamInfo(t["team_id"], t["key"], t["max_budget"], t["spend"])

    async def usage(self, project: Optional[str]) -> List[Dict]:
        rows = self._store.snapshot().get("usage", [])
        if project is None:
            return list(rows)
        return [r for r in rows if r.get("project") == project]

    async def aclose(self) -> None:
        return None


class ProxyBackend:
    """Talks to a running litellm proxy via httpx.AsyncClient."""

    def __init__(self, cfg: Any, store: StateStore):
        self._cfg = cfg
        self._store = store
        base = cfg.litellm.base_url.rstrip("/") + "/"
        timeout_s = int(cfg.litellm.request_timeout_s)
        self._client = httpx.AsyncClient(
            base_url=base,
            headers={
                "Authorization": f"Bearer {cfg.litellm.master_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout_s),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ensure_team(self, project: str, budget_usd: float, duration: str) -> TeamInfo:
        existing = await self.team_info(project)
        if existing:
            return existing

        try:
            team_resp = await self._client.post(
                "team/new",
                json={
                    "team_alias": project,
                    "max_budget": budget_usd,
                    "budget_duration": duration,
                },
            )
            team_resp.raise_for_status()
            team_id = team_resp.json().get("team_id")

            key_resp = await self._client.post(
                "key/generate",
                json={"team_id": team_id, "key_alias": f"llmao-{project}"},
            )
            key_resp.raise_for_status()
            key = key_resp.json().get("key")
        except httpx.TimeoutException as e:
            raise BackendUnavailable(
                f"LiteLLM admin API timed out after {self._cfg.litellm.request_timeout_s}s"
            ) from e
        except httpx.ConnectError as e:
            raise BackendUnavailable(
                f"could not reach LiteLLM at {self._cfg.litellm.base_url}"
            ) from e
        except httpx.HTTPStatusError as e:
            raise BackendUnavailable(
                f"LiteLLM admin error {e.response.status_code}: {e.response.text}"
            ) from e

        def _mut(data):
            data.setdefault("teams", {})[project] = {
                "team_id": team_id, "key": key,
                "max_budget": budget_usd, "spend": 0.0,
                "duration": duration, "created_at": time.time(),
            }
        self._store.update(_mut)
        return TeamInfo(team_id, key, budget_usd, 0.0)

    async def team_info(self, project: str) -> Optional[TeamInfo]:
        t = self._store.snapshot().get("teams", {}).get(project)
        if not t:
            return None
        spend = t.get("spend", 0.0)
        try:
            resp = await self._client.get(
                "team/info",
                params={"team_id": t["team_id"]},
            )
            if resp.is_success:
                spend = resp.json().get("team_info", {}).get("spend", spend)
        except httpx.HTTPError:
            pass
        return TeamInfo(t["team_id"], t["key"], t.get("max_budget", 0.0), spend)

    async def usage(self, project: Optional[str]) -> List[Dict]:
        rows = self._store.snapshot().get("usage", [])
        if project is None:
            return list(rows)
        return [r for r in rows if r.get("project") == project]


def make_backend(cfg: Any, store: StateStore) -> Backend:
    if cfg.litellm.mode != "proxy":
        return MockBackend(cfg, store)
    return ProxyBackend(cfg, store)

"""litellm backend abstraction (control plane).

Two implementations behind one interface:

* ``ProxyBackend`` talks to a real litellm proxy. It uses the proxy's admin
  endpoints (/team/new, /key/generate, /team/info) to provision a team and
  mint a scoped key for each ASF project — this is how per-PMC budgets and
  spend tracking happen natively.

* ``MockBackend`` fakes team provision and usage in-process so the app runs
  with no litellm proxy at all (laptop demos, CI).

Completion traffic is out of scope: clients call LiteLLM directly with
virtual keys. The seam depends only on this interface, so flipping
LLMAO_LITELLM_MODE from "mock" to "proxy" changes nothing upstream.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional, Protocol

from .config import Settings
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
    def ensure_team(self, project: str, budget_usd: float, duration: str) -> TeamInfo: ...
    def team_info(self, project: str) -> Optional[TeamInfo]: ...
    def usage(self, project: Optional[str]) -> List[Dict]: ...


# ---------------------------------------------------------------------------
# Mock backend — no network, used for local/dev/CI.
# ---------------------------------------------------------------------------

class MockBackend:
    def __init__(self, settings: Settings, store: StateStore):
        self._s = settings
        self._store = store

    def ensure_team(self, project: str, budget_usd: float, duration: str) -> TeamInfo:
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

    def team_info(self, project: str) -> Optional[TeamInfo]:
        teams = self._store.snapshot().get("teams", {})
        t = teams.get(project)
        if not t:
            return None
        return TeamInfo(t["team_id"], t["key"], t["max_budget"], t["spend"])

    def usage(self, project: Optional[str]) -> List[Dict]:
        rows = self._store.snapshot().get("usage", [])
        if project is None:
            return list(rows)
        return [r for r in rows if r.get("project") == project]


# ---------------------------------------------------------------------------
# Proxy backend — real litellm proxy over HTTP.
# ---------------------------------------------------------------------------

class ProxyBackend:
    """Talks to a running litellm proxy. Requires `requests`.

    Team provisioning uses the proxy admin API with the master key. Inference
    keys are stored for later key-management work; completions are not proxied
    through this process.
    """

    def __init__(self, settings: Settings, store: StateStore):
        self._s = settings
        self._store = store
        import requests  # local import so mock mode needs no dependency
        self._requests = requests

    def _admin_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._s.litellm_master_key}", "Content-Type": "application/json"}

    def ensure_team(self, project: str, budget_usd: float, duration: str) -> TeamInfo:
        existing = self.team_info(project)
        if existing:
            return existing

        base = self._s.litellm_base_url.rstrip("/")
        # 1. Create a team scoped to this ASF project with a budget.
        team_resp = self._requests.post(
            f"{base}/team/new",
            json={"team_alias": project, "max_budget": budget_usd, "budget_duration": duration},
            headers=self._admin_headers(), timeout=15,
        )
        team_resp.raise_for_status()
        team_id = team_resp.json().get("team_id")

        # 2. Mint a key bound to that team.
        key_resp = self._requests.post(
            f"{base}/key/generate",
            json={"team_id": team_id, "key_alias": f"llmao-{project}"},
            headers=self._admin_headers(), timeout=15,
        )
        key_resp.raise_for_status()
        key = key_resp.json().get("key")

        def _mut(data):
            data.setdefault("teams", {})[project] = {
                "team_id": team_id, "key": key,
                "max_budget": budget_usd, "spend": 0.0,
                "duration": duration, "created_at": time.time(),
            }
        self._store.update(_mut)
        return TeamInfo(team_id, key, budget_usd, 0.0)

    def team_info(self, project: str) -> Optional[TeamInfo]:
        t = self._store.snapshot().get("teams", {}).get(project)
        if not t:
            return None
        # Refresh spend from the proxy when possible.
        spend = t.get("spend", 0.0)
        try:
            base = self._s.litellm_base_url.rstrip("/")
            resp = self._requests.get(
                f"{base}/team/info", params={"team_id": t["team_id"]},
                headers=self._admin_headers(), timeout=10,
            )
            if resp.ok:
                spend = resp.json().get("team_info", {}).get("spend", spend)
        except Exception:
            pass
        return TeamInfo(t["team_id"], t["key"], t.get("max_budget", 0.0), spend)

    def usage(self, project: Optional[str]) -> List[Dict]:
        rows = self._store.snapshot().get("usage", [])
        if project is None:
            return list(rows)
        return [r for r in rows if r.get("project") == project]


def make_backend(settings: Settings, store: StateStore) -> Backend:
    if settings.is_mock_llm:
        return MockBackend(settings, store)
    return ProxyBackend(settings, store)

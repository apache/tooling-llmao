"""LiteLLM admin backend (async httpx).

LiteLLMBackend talks to a real LiteLLM proxy. The running app always uses this
client. Tests inject a mock from tests/mock_backend.py — never selected by config.
PATs are virtual keys; list/create/delete go through admin APIs. Team ensure
stores team_id only (not a product PAT).
"""
from __future__ import annotations

import time
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
    max_budget: float = 0.0
    spend: float = 0.0
    # Optional legacy field; product PATs are not stored here.
    key: str = ""


@dataclass
class KeyInfo:
    """PAT view model: project + user + purpose (design §5.1).

    Maps from LiteLLM wire fields only at the boundary. ``token_id`` is the
    LiteLLM ``token`` value used for list/delete — not the one-time ``sk-…``
    secret (that lives on CreatedKey.secret only).
    """
    token_id: str
    project: str                 # metadata.project (LDAP name)
    user: Optional[str]          # LiteLLM user_id; None = automation
    purpose: str                 # LiteLLM key_alias
    team_id: str                 # LiteLLM opaque team id (API only)
    spend: float
    max_budget: Optional[float]
    created_at: Optional[str]
    last_used: Optional[str]
    blocked: bool = False

    @property
    def is_automation(self) -> bool:
        return self.user is None


@dataclass
class CreatedKey:
    """Result of minting a key — includes secret once."""
    secret: str
    info: KeyInfo


class Backend(Protocol):
    async def ensure_team(self, project: str, budget_usd: float, duration: str) -> TeamInfo: ...
    async def team_info(self, project: str) -> Optional[TeamInfo]: ...
    async def list_keys(
        self,
        *,
        user: Optional[str] = None,
        team_id: Optional[str] = None,
        size: int = 100,
    ) -> List[KeyInfo]: ...
    async def create_key(
        self,
        *,
        team_id: str,
        purpose: str,
        user: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> CreatedKey: ...
    async def delete_key(self, token_id: str) -> None: ...
    async def usage(self, project: Optional[str]) -> List[Dict]: ...
    async def aclose(self) -> None: ...


def _normalize_key_obj(raw: Any) -> KeyInfo:
    """Build KeyInfo from a LiteLLM key object (wire → design names)."""
    if isinstance(raw, str):
        raise ValueError(
            "key object is a bare token string; need full object with metadata.project"
        )
    if not isinstance(raw, dict):
        raw = dict(raw) if hasattr(raw, "items") else {}
    meta = raw.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    project = meta.get("project")
    if not project:
        raise ValueError(
            "key missing metadata.project (ASF LDAP project name required on every key)"
        )
    project = str(project)

    user_raw = raw.get("user_id")
    user = str(user_raw) if user_raw else None
    purpose = str(raw.get("key_alias") or "")
    if not purpose:
        raise ValueError("key missing key_alias (purpose)")

    last = (
        raw.get("last_used")
        or raw.get("last_active")
        or raw.get("updated_at")
        or raw.get("last_refreshed_at")
    )
    if last is not None and not isinstance(last, str):
        last = str(last)
    created = raw.get("created_at")
    if created is not None and not isinstance(created, str):
        created = str(created)
    token_id = raw.get("token") or raw.get("token_id") or ""
    if not token_id:
        raise ValueError("key missing token / token_id")
    return KeyInfo(
        token_id=str(token_id),
        project=project,
        user=user,
        purpose=purpose,
        team_id=str(raw.get("team_id") or ""),
        spend=float(raw.get("spend") or 0.0),
        max_budget=(
            float(raw["max_budget"])
            if raw.get("max_budget") is not None
            else None
        ),
        created_at=created,
        last_used=last,
        blocked=bool(raw.get("blocked")),
    )


class LiteLLMBackend:
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

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            resp = await self._client.request(method, path, **kwargs)
            return resp
        except httpx.TimeoutException as e:
            raise BackendUnavailable(
                f"LiteLLM admin API timed out after {self._cfg.litellm.request_timeout_s}s"
            ) from e
        except httpx.ConnectError as e:
            raise BackendUnavailable(
                f"could not reach LiteLLM at {self._cfg.litellm.base_url}"
            ) from e

    def _raise_http(self, resp: httpx.Response) -> None:
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise BackendUnavailable(
                f"LiteLLM admin error {e.response.status_code}: {e.response.text}"
            ) from e

    def _remember_team(self, project: str, team_id: str, budget_usd: float) -> None:
        def _mut(data):
            data.setdefault("teams", {})[project] = {
                "team_id": team_id,
                "max_budget": budget_usd,
                "spend": 0.0,
                "created_at": time.time(),
            }
        self._store.update(_mut)

    async def _find_team_id_by_alias(self, project: str) -> Optional[str]:
        resp = await self._request("GET", "team/list")
        if not resp.is_success:
            return None
        body = resp.json()
        teams = body if isinstance(body, list) else body.get("teams") or body.get("data") or []
        for t in teams:
            if isinstance(t, dict) and t.get("team_alias") == project:
                return t.get("team_id")
            if hasattr(t, "team_alias") and getattr(t, "team_alias", None) == project:
                return getattr(t, "team_id", None)
        return None

    async def ensure_team(self, project: str, budget_usd: float, duration: str) -> TeamInfo:
        existing = await self.team_info(project)
        if existing:
            return existing

        # Already on LiteLLM but not in local map?
        found = await self._find_team_id_by_alias(project)
        if found:
            self._remember_team(project, found, budget_usd)
            return TeamInfo(found, budget_usd, 0.0)

        resp = await self._request(
            "POST",
            "team/new",
            json={
                "team_alias": project,
                "max_budget": budget_usd,
                "budget_duration": duration,
            },
        )
        if resp.status_code >= 400:
            # Race / already exists
            found = await self._find_team_id_by_alias(project)
            if found:
                self._remember_team(project, found, budget_usd)
                return TeamInfo(found, budget_usd, 0.0)
            self._raise_http(resp)

        team_id = resp.json().get("team_id")
        if not team_id:
            raise BackendUnavailable("LiteLLM team/new returned no team_id")
        self._remember_team(project, team_id, budget_usd)
        return TeamInfo(team_id, budget_usd, 0.0)

    async def team_info(self, project: str) -> Optional[TeamInfo]:
        t = self._store.snapshot().get("teams", {}).get(project)
        if not t:
            return None
        spend = t.get("spend", 0.0)
        max_budget = t.get("max_budget", 0.0)
        try:
            resp = await self._request(
                "GET",
                "team/info",
                params={"team_id": t["team_id"]},
            )
            if resp.is_success:
                info = resp.json().get("team_info") or resp.json()
                if isinstance(info, dict):
                    spend = info.get("spend", spend)
                    max_budget = info.get("max_budget", max_budget)
        except BackendUnavailable:
            pass
        return TeamInfo(t["team_id"], float(max_budget or 0), float(spend or 0))

    async def list_keys(
        self,
        *,
        user: Optional[str] = None,
        team_id: Optional[str] = None,
        size: int = 100,
    ) -> List[KeyInfo]:
        params: Dict[str, Any] = {
            "page": 1,
            "size": size,
            "return_full_object": "true",
        }
        if user:
            params["user_id"] = user
        if team_id:
            params["team_id"] = team_id
        resp = await self._request("GET", "key/list", params=params)
        self._raise_http(resp)
        body = resp.json()
        keys = body.get("keys") if isinstance(body, dict) else body
        if not keys:
            return []
        try:
            return [_normalize_key_obj(k) for k in keys]
        except ValueError as e:
            raise BackendUnavailable(str(e)) from e

    async def create_key(
        self,
        *,
        team_id: str,
        purpose: str,
        user: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> CreatedKey:
        meta = dict(metadata or {})
        if not meta.get("project"):
            raise BackendUnavailable(
                "create_key requires metadata.project (ASF LDAP project name)"
            )
        payload: Dict[str, Any] = {
            "team_id": team_id,
            "key_alias": purpose,
            "metadata": meta,
        }
        if user:
            payload["user_id"] = user
        resp = await self._request("POST", "key/generate", json=payload)
        self._raise_http(resp)
        body = resp.json()
        secret = body.get("key") or body.get("token")
        if not secret:
            raise BackendUnavailable("LiteLLM key/generate returned no key secret")
        info_src = body.get("info") or body
        if isinstance(info_src, dict) and not info_src.get("token") and not info_src.get("token_id"):
            info_src = {
                **info_src,
                "token": body.get("token_id") or body.get("token") or secret,
            }
        if isinstance(info_src, dict):
            merged = {**info_src}
            src_meta = merged.get("metadata")
            if not isinstance(src_meta, dict):
                src_meta = {}
            merged["metadata"] = {**meta, **src_meta, "project": meta["project"]}
            if not merged.get("key_alias"):
                merged["key_alias"] = purpose
            if not merged.get("team_id"):
                merged["team_id"] = team_id
            if user and not merged.get("user_id"):
                merged["user_id"] = user
            info_src = merged
        try:
            info = _normalize_key_obj(info_src)
        except ValueError as e:
            raise BackendUnavailable(str(e)) from e
        return CreatedKey(secret=str(secret), info=info)

    async def delete_key(self, token_id: str) -> None:
        resp = await self._request("POST", "key/delete", json={"keys": [token_id]})
        self._raise_http(resp)

    async def usage(self, project: Optional[str]) -> List[Dict]:
        rows = self._store.snapshot().get("usage", [])
        if project is None:
            return list(rows)
        return [r for r in rows if r.get("project") == project]


def make_backend(cfg: Any, store: StateStore) -> Backend:
    return LiteLLMBackend(cfg, store)

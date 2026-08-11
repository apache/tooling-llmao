"""LiteLLM admin backend (async httpx).

LiteLLMBackend talks to a real LiteLLM proxy. The running app always uses this
client. Tests inject a mock from tests/mock_backend.py — never selected by config.

Product API speaks LDAP **project** names. Mapping project (team_alias) →
LiteLLM opaque team_id is internal to LiteLLMBackend only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

import httpx


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
    purpose: str                 # metadata.purpose (optional label; may be "")
    team_id: str                 # LiteLLM opaque team id (API only)
    spend: float
    max_budget: Optional[float]
    created_at: Optional[str]
    last_used: Optional[str]
    # metadata.created_by — who minted and saw the secret (required if automation)
    created_by: Optional[str] = None
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
    async def team_info(self, project: str) -> Optional[TeamInfo]: ...
    async def list_keys(
        self,
        *,
        user: Optional[str] = None,
        project: Optional[str] = None,
        size: int = 100,
    ) -> List[KeyInfo]: ...
    async def create_key(
        self,
        *,
        project: str,
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
    purpose = str(meta.get("purpose") or "")

    created_by_raw = meta.get("created_by")
    created_by = str(created_by_raw) if created_by_raw else None
    # Automation keys have no user_id; only the minter saw the secret.
    if user is None and not created_by:
        raise ValueError(
            "automation key missing metadata.created_by (uid who minted the secret)"
        )

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
        created_by=created_by,
        blocked=bool(raw.get("blocked")),
    )


class LiteLLMBackend:
    """Talks to a running litellm proxy via httpx.AsyncClient.

    In-process cache is only project (team_alias) → team_id. Spend/budget are
    never taken from the cache. Call ``warm()`` at process start (fail-fast).
    """

    def __init__(self, cfg: Any):
        self._cfg = cfg
        self._team_ids: Dict[str, str] = {}  # project → team_id
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

    def _ingest_team_list(self, teams: List[Any]) -> List[Dict[str, Any]]:
        """Update project→team_id cache from team/list rows; return dict rows."""
        rows: List[Dict[str, Any]] = []
        for t in teams:
            if hasattr(t, "model_dump"):
                d = t.model_dump()
            elif hasattr(t, "dict"):
                d = t.dict()
            elif isinstance(t, dict):
                d = t
            else:
                continue
            rows.append(d)
            alias = d.get("team_alias")
            tid = d.get("team_id")
            if alias and tid:
                self._team_ids[str(alias)] = str(tid)
        return rows

    async def _team_list_rows(self) -> List[Dict[str, Any]]:
        resp = await self._request("GET", "team/list")
        self._raise_http(resp)
        body = resp.json()
        teams = body if isinstance(body, list) else body.get("teams") or body.get("data") or []
        if not isinstance(teams, list):
            teams = []
        return self._ingest_team_list(teams)

    @staticmethod
    def _team_info_from_row(row: Dict[str, Any]) -> TeamInfo:
        return TeamInfo(
            str(row.get("team_id") or ""),
            float(row.get("max_budget") or 0),
            float(row.get("spend") or 0),
        )

    async def warm(self) -> None:
        """Load project→team_id from LiteLLM. Fail-fast if the proxy is unreachable."""
        await self._team_list_rows()

    async def ensure_team_id(self, project: str) -> str:
        """Map LDAP project (team_alias) → LiteLLM team_id; create team if needed."""
        project = (project or "").strip()
        if not project:
            raise BackendUnavailable("project is required")
        if project in self._team_ids:
            return self._team_ids[project]

        rows = await self._team_list_rows()
        if project in self._team_ids:
            return self._team_ids[project]

        budget_usd = float(self._cfg.budgets.default_team_budget_usd)
        duration = self._cfg.budgets.duration
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
            await self._team_list_rows()
            if project in self._team_ids:
                return self._team_ids[project]
            self._raise_http(resp)

        team_id = resp.json().get("team_id")
        if not team_id:
            raise BackendUnavailable("LiteLLM team/new returned no team_id")
        self._team_ids[project] = str(team_id)
        return str(team_id)

    async def team_info(self, project: str) -> Optional[TeamInfo]:
        """Live team spend/budget. Cache holds ids only; spend is never cached.

        Cache hit → team/info. Cache miss → team/list (fills cache + returns
        list-row fields; no second team/info).
        """
        project = (project or "").strip()
        if not project:
            return None

        if project in self._team_ids:
            team_id = self._team_ids[project]
            resp = await self._request(
                "GET",
                "team/info",
                params={"team_id": team_id},
            )
            self._raise_http(resp)
            body = resp.json()
            info = body.get("team_info") or body
            if not isinstance(info, dict):
                info = {}
            return TeamInfo(
                team_id,
                float(info.get("max_budget") or 0),
                float(info.get("spend") or 0),
            )

        rows = await self._team_list_rows()
        for row in rows:
            if str(row.get("team_alias") or "") == project:
                return self._team_info_from_row(row)
        return None

    async def list_keys(
        self,
        *,
        user: Optional[str] = None,
        project: Optional[str] = None,
        size: int = 100,
    ) -> List[KeyInfo]:
        params: Dict[str, Any] = {
            "page": 1,
            "size": size,
            "return_full_object": "true",
        }
        if user:
            params["user_id"] = user
        if project:
            if project not in self._team_ids:
                await self._team_list_rows()
            team_id = self._team_ids.get(project)
            if not team_id:
                return []
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
        project: str,
        purpose: str,
        user: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> CreatedKey:
        project = (project or "").strip()
        if not project:
            raise BackendUnavailable("create_key requires project")
        team_id = await self.ensure_team_id(project)
        purpose = (purpose or "").strip()
        meta = dict(metadata or {})
        meta["project"] = project
        if purpose:
            meta["purpose"] = purpose
        payload: Dict[str, Any] = {
            "team_id": team_id,
            "metadata": meta,
            # key_alias is globally unique in LiteLLM — do not put purpose there.
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
            merged["metadata"] = {**meta, **src_meta, "project": project}
            if purpose:
                merged["metadata"]["purpose"] = purpose
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
        # Spend APIs not wired yet.
        return []

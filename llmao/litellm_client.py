# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""LiteLLM admin backend (async httpx).

LiteLLMBackend talks to a real LiteLLM proxy. The running app always uses this
client. Tests inject a mock from tests/mock_backend.py — never selected by config.

Product API speaks LDAP **project** names. Mapping project (team_alias) →
LiteLLM opaque team_id is internal to LiteLLMBackend only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from llmao.fleet import VllmServer

_LOGGER = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """Raised when a team is over budget (mirrors litellm proxy's 4xx)."""


class BackendUnavailableError(Exception):
    """Raised when the litellm admin API times out or can't be reached."""


# Who authorized the project's dollar ceiling. First cfg default → Free Tier.
GRANTOR_FREE_TIER = "Free Tier"


@dataclass
class TeamInfo:
    """LiteLLM team budget facts for an ASF project (mapped via team_alias).

    ``budget_duration`` is always a concrete string (e.g. ``30d``), resolved
    with ``resolve_budget_duration`` (LiteLLM field → cfg.budgets.duration).

    ``grantor`` is ``metadata.grantor`` on the team (LiteLLM /team/new accepts
    metadata, same as keys). Missing → Free Tier.

    TODO(investigate): confirm LiteLLM team/info always returns budget_duration
    (or equivalent); keep cfg fallback required so product never sees Optional.
    """

    team_id: str
    max_budget: float = 0.0
    spend: float = 0.0
    budget_duration: str = ""
    grantor: str = GRANTOR_FREE_TIER
    # Optional legacy field; product PATs are not stored here.
    key: str = ""


def resolve_grantor(raw: Any, metadata: Any = None) -> str:
    """Team metadata.grantor, or Free Tier if unset."""
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    if isinstance(metadata, dict):
        g = metadata.get("grantor")
        if g is not None and str(g).strip():
            return str(g).strip()
    return GRANTOR_FREE_TIER


def resolve_budget_duration(raw: Any, cfg: Any) -> str:
    """Prefer LiteLLM team field; fall back to cfg.budgets.duration. Fail-fast if both empty."""
    if raw is not None:
        s = str(raw).strip()
        if s:
            return s
    budgets = getattr(cfg, "budgets", None)
    fallback = getattr(budgets, "duration", None) if budgets is not None else None
    if fallback is not None:
        s = str(fallback).strip()
        if s:
            return s
    raise BackendUnavailableError("budget_duration missing from LiteLLM team and cfg.budgets.duration is unset")


@dataclass
class KeyInfo:
    """PAT view model: project + user + purpose (design §5.1).

    Maps from LiteLLM wire fields only at the boundary. ``token_id`` is the
    LiteLLM ``token`` value used for list/delete — not the one-time ``sk-…``
    secret (that lives on CreatedKey.secret only).
    """

    token_id: str
    project: str  # metadata.project (LDAP name)
    user: str | None  # LiteLLM user_id; None = automation
    purpose: str  # metadata.purpose (optional label; may be "")
    team_id: str  # LiteLLM opaque team id (API only)
    spend: float
    max_budget: float | None
    created_at: str | None
    last_used: str | None
    # metadata.created_by — who minted and saw the secret (required if automation)
    created_by: str | None = None
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
    async def team_info(self, project: str) -> TeamInfo | None: ...
    async def ensure_team(self, project: str) -> TeamInfo: ...
    async def list_keys(
        self,
        *,
        user: str | None = None,
        project: str | None = None,
        size: int = 100,
    ) -> list[KeyInfo]: ...
    async def create_key(
        self,
        *,
        project: str,
        purpose: str,
        user: str | None = None,
        metadata: dict | None = None,
    ) -> CreatedKey: ...
    async def delete_key(self, token_id: str) -> None: ...
    async def usage(self, project: str | None) -> list[dict]: ...
    async def aclose(self) -> None: ...


def _normalize_key_obj(raw: Any) -> KeyInfo:
    """Build KeyInfo from a LiteLLM key object (wire → design names)."""
    if isinstance(raw, str):
        raise ValueError("key object is a bare token string; need full object with metadata.project")
    if not isinstance(raw, dict):
        raw = dict(raw) if hasattr(raw, "items") else {}
    meta = raw.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    project = meta.get("project")
    if not project:
        raise ValueError("key missing metadata.project (ASF LDAP project name required on every key)")
    project = str(project)

    user_raw = raw.get("user_id")
    user = str(user_raw) if user_raw else None
    purpose = str(meta.get("purpose") or "")

    created_by_raw = meta.get("created_by")
    created_by = str(created_by_raw) if created_by_raw else None
    # Automation keys have no user_id; only the minter saw the secret.
    if user is None and not created_by:
        raise ValueError("automation key missing metadata.created_by (uid who minted the secret)")

    last = raw.get("last_used") or raw.get("last_active") or raw.get("updated_at") or raw.get("last_refreshed_at")
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
        max_budget=(float(raw["max_budget"]) if raw.get("max_budget") is not None else None),
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

    def __init__(self, cfg: Any, fleet: Any):
        self._cfg = cfg
        self.fleet = fleet
        self._team_ids: dict[str, str] = {}  # project → team_id
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
            raise BackendUnavailableError(
                f"LiteLLM admin API timed out after {self._cfg.litellm.request_timeout_s}s"
            ) from e
        except httpx.ConnectError as e:
            raise BackendUnavailableError(f"could not reach LiteLLM at {self._cfg.litellm.base_url}") from e

    def _raise_http(self, resp: httpx.Response) -> None:
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise BackendUnavailableError(f"LiteLLM admin error {e.response.status_code}: {e.response.text}") from e

    def _ingest_team_list(self, teams: list[Any]) -> list[dict[str, Any]]:
        """Update project→team_id cache from team/list rows; return dict rows."""
        rows: list[dict[str, Any]] = []
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

    async def _team_list_rows(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "team/list")
        self._raise_http(resp)
        body = resp.json()
        teams = body if isinstance(body, list) else body.get("teams") or body.get("data") or []
        if not isinstance(teams, list):
            teams = []
        return self._ingest_team_list(teams)

    def _team_info_from_dict(self, d: dict[str, Any], *, team_id: str | None = None) -> TeamInfo:
        tid = team_id if team_id is not None else str(d.get("team_id") or "")
        raw_dur = d.get("budget_duration")
        if raw_dur is None or raw_dur == "":
            raw_dur = d.get("duration")
        meta = d.get("metadata")
        if not isinstance(meta, dict):
            meta = {}
        return TeamInfo(
            tid,
            float(d.get("max_budget") or 0),
            float(d.get("spend") or 0),
            budget_duration=resolve_budget_duration(raw_dur, self._cfg),
            grantor=resolve_grantor(d.get("grantor"), meta),
        )

    async def warm(self) -> None:
        """Load project→team_id from LiteLLM. Fail-fast if the proxy is unreachable."""
        await self._team_list_rows()
        await self.ensure_commercial()

    async def ensure_team_id(self, project: str) -> str:
        """Map LDAP project (team_alias) → LiteLLM team_id; create team if needed."""
        info = await self.ensure_team(project)
        return info.team_id

    async def ensure_team(self, project: str) -> TeamInfo:
        """Ensure a LiteLLM team exists for the project; return live budget facts."""
        project = (project or "").strip()
        if not project:
            raise BackendUnavailableError("project is required")

        existing = await self.team_info(project)
        if existing is not None:
            return existing

        budget_usd = float(self._cfg.budgets.default_team_budget_usd)
        duration = resolve_budget_duration(None, self._cfg)
        resp = await self._request(
            "POST",
            "team/new",
            json={
                "team_alias": project,
                "max_budget": budget_usd,
                "budget_duration": duration,
                "metadata": {"grantor": GRANTOR_FREE_TIER},
            },
        )
        if resp.status_code >= 400:
            again = await self.team_info(project)
            if again is not None:
                return again
            self._raise_http(resp)

        body = resp.json() if resp.content else {}
        if not isinstance(body, dict):
            body = {}
        team_id = body.get("team_id")
        if not team_id:
            raise BackendUnavailableError("LiteLLM team/new returned no team_id")
        self._team_ids[project] = str(team_id)
        live = await self.team_info(project)
        if live is not None:
            return live
        return self._team_info_from_dict(
            {
                **body,
                "max_budget": body.get("max_budget", budget_usd),
                "spend": body.get("spend", 0),
            },
            team_id=str(team_id),
        )

    async def team_info(self, project: str) -> TeamInfo | None:
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
            return self._team_info_from_dict(info, team_id=team_id)

        rows = await self._team_list_rows()
        for row in rows:
            if str(row.get("team_alias") or "") == project:
                return self._team_info_from_dict(row)
        return None

    async def list_keys(
        self,
        *,
        user: str | None = None,
        project: str | None = None,
        size: int = 100,
    ) -> list[KeyInfo]:
        params: dict[str, Any] = {
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
            raise BackendUnavailableError(str(e)) from e

    async def create_key(
        self,
        *,
        project: str,
        purpose: str,
        user: str | None = None,
        metadata: dict | None = None,
    ) -> CreatedKey:
        project = (project or "").strip()
        if not project:
            raise BackendUnavailableError("create_key requires project")
        team = await self.ensure_team(project)
        team_id = team.team_id
        purpose = (purpose or "").strip()
        meta = dict(metadata or {})
        meta["project"] = project
        if purpose:
            meta["purpose"] = purpose
        payload: dict[str, Any] = {
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
            raise BackendUnavailableError("LiteLLM key/generate returned no key secret")
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
            raise BackendUnavailableError(str(e)) from e
        return CreatedKey(secret=str(secret), info=info)

    async def delete_key(self, token_id: str) -> None:
        resp = await self._request("POST", "key/delete", json={"keys": [token_id]})
        self._raise_http(resp)

    async def usage(self, project: str | None) -> list[dict]:
        # Spend APIs not wired yet.
        return []

    def deployment_body(self, dep) -> dict:
        """POST /model/new payload from the catalog + this deployment's api_base."""
        entry = self.fleet.catalog.get(dep.model_name)
        if entry is None:
            raise BackendUnavailableError(f"catalog missing {dep.model_name}; cannot POST /model/new")
        params = dict(entry.litellm_params)
        if not dep.api_base:
            raise BackendUnavailableError(f"{dep.name}: no api_base for /model/new")
        params["api_base"] = dep.api_base
        if dep.self_hosted and dep.vllm is not None and dep.vllm.api_key:
            params["api_key"] = dep.vllm.api_key
        return {
            "model_name": dep.model_name,
            "litellm_params": params,
            "model_info": {
                "self_hosted": dep.self_hosted,
                "asf_api_base": _norm_base(dep.api_base),
            },
        }

    async def _model_info_rows(self) -> list:
        resp = await self._request("GET", "model/info")
        self._raise_http(resp)
        body = resp.json()
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            return body.get("data") or body.get("models") or []
        return []

    def _litellm_id(self, row: dict, dep) -> str | None:
        if str(row.get("model_name") or "") != dep.model_name:
            return None
        want = _norm_base(dep.api_base)
        info = row.get("model_info") or {}
        if not isinstance(info, dict):
            info = {}
        if (
            _norm_base(info.get("asf_api_base")) == want
            or _norm_base(
                (row.get("litellm_params") or {}).get("api_base") if isinstance(row.get("litellm_params"), dict) else ""
            )
            == want
        ):
            return str(info.get("id") or row.get("model_id") or "") or None
        return None

    async def add_deployment(self, dep) -> None:
        body = self.deployment_body(dep)
        resp = await self._request("POST", "model/new", json=body)
        if resp.status_code in (400, 409):
            dep.in_litellm = True
            _LOGGER.info("model/new already present %s@%s", dep.name, dep.api_base)
            return
        self._raise_http(resp)
        dep.in_litellm = True
        _LOGGER.info("model/new %s@%s", dep.name, dep.api_base)

    async def delete_deployment(self, dep) -> None:
        rows = await self._model_info_rows()
        found = None
        for row in rows:
            if isinstance(row, dict):
                found = self._litellm_id(row, dep)
                if found:
                    break
        if not found:
            dep.in_litellm = False
            _LOGGER.warning("model/delete: no LiteLLM id for %s@%s", dep.name, dep.api_base)
            return
        resp = await self._request("POST", "model/delete", json={"id": found})
        self._raise_http(resp)
        dep.in_litellm = False
        _LOGGER.info("model/delete %s@%s id=%s", dep.name, dep.api_base, found)

    async def ensure_commercial(self) -> None:
        """At startup: POST /model/new for each commercial catalog deployment."""
        for dep in self.fleet.deployments:
            if dep.self_hosted or not dep.api_base:
                continue
            if dep.in_litellm:
                continue
            await self.add_deployment(dep)

    async def sync_selfhost(self) -> None:
        """After vLLM probes: register serving backends, drop down ones."""
        for dep in self.fleet.deployments:
            if not dep.self_hosted or dep.vllm is None:
                continue
            srv = dep.vllm
            if srv.state == VllmServer.SERVING and dep.api_base and not dep.in_litellm:
                await self.add_deployment(dep)
            elif srv.state == VllmServer.DOWN and dep.in_litellm:
                await self.delete_deployment(dep)

    async def run_skew(self) -> None:
        """Config skew often; LiteLLM GET /health (tokens) on a long interval."""
        skew_s = float(self._cfg.fleet.skew_interval_s)
        health_s = float(self._cfg.fleet.litellm_health_interval_s)
        last_health = 0.0
        first = True
        while True:
            try:
                await self.check_config_skew()
                now = time.time()
                if first or (now - last_health) >= health_s:
                    await self.check_health_skew()
                    last_health = now
                    first = False
            except Exception:
                _LOGGER.exception("LiteLLM skew check failed")
            await asyncio.sleep(skew_s)

    async def check_config_skew(self) -> None:
        resp = await self._request("GET", "model/info")
        self._raise_http(resp)
        bases = _api_bases_from_model_info(resp.json())
        intended = {_norm_base(d.api_base) for d in self.fleet.deployments if d.api_base}
        for dep in self.fleet.deployments:
            note = "missing from LiteLLM"
            base = _norm_base(dep.api_base) if dep.api_base else None
            if base and base in bases:
                dep.in_litellm = True
                dep.skew = [n for n in dep.skew if n != note]
            else:
                dep.in_litellm = False
                if base and note not in dep.skew:
                    dep.skew.append(note)
                    _LOGGER.warning("skew: %s@%s %s", dep.name, dep.api_base, note)
        extra = bases - intended
        if extra:
            _LOGGER.warning("skew: LiteLLM api_base not an intended deployment: %s", sorted(extra))

    async def check_health_skew(self) -> None:
        resp = await self._request("GET", "health")
        self._raise_http(resp)
        body = resp.json() if resp.content else {}
        healthy = {
            _norm_base(x.get("api_base"))
            for x in (body.get("healthy_endpoints") or [])
            if isinstance(x, dict) and x.get("api_base")
        }
        unhealthy = {
            _norm_base(x.get("api_base"))
            for x in (body.get("unhealthy_endpoints") or [])
            if isinstance(x, dict) and x.get("api_base")
        }
        stamp = time.time()
        for dep in self.fleet.deployments:
            base = _norm_base(dep.api_base) if dep.api_base else None
            litellm_up = bool(base) and base in healthy
            litellm_down = bool(base) and base in unhealthy
            if litellm_up:
                dep.litellm_healthy = True
                dep.litellm_health_at = stamp
            elif litellm_down:
                dep.litellm_healthy = False
                dep.litellm_health_at = stamp
            else:
                dep.litellm_healthy = None
            srv = dep.vllm
            if srv is None:
                continue
            note = "LiteLLM health disagrees"
            disagrees = (srv.state == VllmServer.SERVING and litellm_down) or (
                srv.state == VllmServer.DOWN and litellm_up
            )
            if disagrees:
                if note not in dep.skew:
                    dep.skew.append(note)
                _LOGGER.warning(
                    "health skew: %s@%s fleet=%s litellm_up=%s",
                    dep.name,
                    dep.api_base,
                    srv.state,
                    litellm_up,
                )
            else:
                dep.skew = [n for n in dep.skew if n != note]


def _norm_base(url: Any) -> str:
    """Strip trailing slashes and a trailing /v1.

    The identity the skew check compares on: the same route appears with the
    suffix (VllmServer.api_base, and the litellm_params.api_base copied from
    it) and without (asf_api_base, routes pushed before the suffix). The
    comparison must match across both forms.

    (The suffix was once actively wrong: an older LiteLLM appended
    /v1/chat/completions to api_base, so a /v1-suffixed base resolved to
    /v1/v1/..., 404'd at the origin, and surfaced as an OpenAI
    authentication error naming platform.openai.com -- which sends the reader
    after their own key rather than the route. The current LiteLLM appends
    only /chat/completions, so the suffix is what vLLM needs now.)
    """
    s = str(url or "").rstrip("/")
    if s.endswith("/v1"):
        s = s[:-3].rstrip("/")
    return s


def _api_bases_from_model_info(body: Any) -> set[str]:
    rows = []
    if isinstance(body, list):
        rows = body
    elif isinstance(body, dict):
        rows = body.get("data") or body.get("models") or []
    bases: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        params = row.get("litellm_params") or {}
        if isinstance(params, dict) and params.get("api_base"):
            bases.add(_norm_base(params["api_base"]))
    return bases

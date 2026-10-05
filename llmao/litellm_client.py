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
import datetime as _dt
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from llmao.fleet import ExtraLiteLLM, snapshot_for_status
from llmao.model_status import (
    HEALTHY,
    SKEW_IN_LITELLM_NOT_IN_CONFIG,
    SKEW_PHRASE,
    STALLED,
    UNHEALTHY,
    DeploymentSnapshot,
    deployment_status,
)
from llmao.token_window import RollingAllowance

_LOGGER = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """Raised when a team is over budget (mirrors litellm proxy's 4xx)."""


class BackendUnavailableError(Exception):
    """Raised when the litellm admin API times out or can't be reached."""


# Who authorized the project's dollar ceiling. First cfg default → Free Tier.
GRANTOR_FREE_TIER = "Free Tier"

# Key tiers. The name is stored in key metadata so a later audit can tell what
# a key was minted as, and so /key/update can re-apply the right ceiling.
TIER_FREE = "free_tier"
TIER_PROJECT = "project"
TIER_SERVICE = "service"

# LiteLLM fields these map to. Named here so the mapping is in one place
# rather than spread across call sites.
_LIMIT_FIELDS = ("rpm_limit", "tpm_limit", "max_parallel_requests")


def resolve_entitlement(cfg: Any, tier: str) -> dict[str, Any]:
    """Limits for a tier, falling back to entitlements.default.

    Returns only the fields that are set, so a tier can raise one ceiling
    without having to restate the others.

    Absent configuration yields an empty dict and therefore an unlimited key.
    That is LiteLLM's behaviour, not a choice -- which is why config.yaml
    ships with values and why this is worth asserting in tests.
    """
    ents = getattr(cfg, "entitlements", None)
    if ents is None:
        return {}
    base = getattr(ents, "default", None)
    tier_cfg = getattr(ents, tier, None)
    out: dict[str, Any] = {}
    for src in (base, tier_cfg):
        if src is None:
            continue
        for name in _LIMIT_FIELDS:
            val = getattr(src, name, None)
            if val is not None:
                out[name] = val
    return out


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
    # Weekly token allocation, in metadata rather than a LiteLLM field --
    # LiteLLM meters dollars and this is capacity. Zero means unset, which
    # renders as "no cap" rather than as a cap of nothing.
    token_cap: int = 0
    token_window_days: int = 7
    # The team's raw metadata dict.
    #
    # Needed because several things are stored there -- grantor, the token
    # allocation, the reserved team's tier overrides -- and a caller updating
    # one must merge rather than replace, or it silently drops the others.
    # Until this existed, `getattr(info, "metadata", None) or {}` returned {}
    # at every call site and the tier overrides were written but never read.
    metadata: dict[str, Any] = field(default_factory=dict)


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
    async def set_tier_entitlement(
        self,
        tier: str,
        *,
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        max_parallel_requests: int | None = None,
        token_cap: int | None = None,
        token_window_days: int | None = None,
    ) -> dict[str, Any]: ...
    async def tier_entitlement(self, tier: str) -> dict[str, Any]: ...
    async def update_team_entitlement(
        self,
        project: str,
        *,
        max_budget_usd: float | None = None,
        token_cap: int | None = None,
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        max_parallel_requests: int | None = None,
    ) -> TeamInfo: ...
    async def update_key_entitlement(
        self,
        token_id: str,
        *,
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        max_parallel_requests: int | None = None,
        max_budget_usd: float | None = None,
    ) -> None: ...
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
        tier: str = TIER_PROJECT,
    ) -> CreatedKey: ...
    async def delete_key(self, token_id: str) -> None: ...
    async def usage(self, project: str | None) -> list[dict]: ...
    async def key_usage(self, user: str) -> dict[str, int]: ...
    def invalidate_spend(self) -> None: ...
    async def allowance(self, user: str, *, tier: str = TIER_FREE) -> RollingAllowance: ...
    def invalidate_allowance(self, user: str | None = None) -> None: ...
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
        # user → (monotonic stamp, daily token buckets). Allowances are
        # derived from the spend log on read; this keeps a page refresh from
        # costing a scan.
        self._allowance_cache: dict[str, tuple[float, dict[str, int]]] = {}
        # (monotonic stamp, folded spend rows) for the current week.
        self._spend_cache: tuple[float, list[dict]] | None = None
        # model_name -> cost_basis, filled when routes are registered, so a
        # spend row can be told from an invoice without a second lookup.
        self._cost_basis: dict[str, str] = {}
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
            token_cap=int(meta.get("token_cap") or 0),
            token_window_days=int(meta.get("token_window_days") or 7),
            metadata=meta,
        )

    async def warm(self) -> None:
        """Load project→team_id from LiteLLM. Fail-fast if the proxy is unreachable."""
        rows = await self._team_list_rows()
        await self._backfill_token_caps(rows)
        await self.ensure_commercial()

    async def _backfill_token_caps(self, rows: list[dict[str, Any]]) -> None:
        """Give every team without a token allocation the configured default.

        Projects come from LDAP. Nobody sets a project up, so there is no
        moment at which someone could be asked for its allocation --
        ensure_team writes the default when it first sees a new PMC, which
        covers everything from now on but not the teams that already exist,
        and not one created while an older build was running.

        So this runs at every startup rather than being a one-off script: a
        migration that only works if someone remembers to run it is one that
        silently misses the project created last Tuesday.

        Idempotent. The test is on the token_cap key being ABSENT rather than
        on its value, so a project an admin deliberately capped at something
        unusual -- including zero -- is left alone.

        Failures are logged and swallowed: an unreachable team should not stop
        the portal booting, and the next restart retries.
        """
        default = int(getattr(self._cfg.budgets, "default_team_token_cap", 0) or 0)
        if default <= 0:
            return

        patched = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            alias = row.get("team_alias")
            team_id = row.get("team_id")
            if not alias or not team_id:
                continue
            meta = row.get("metadata")
            if not isinstance(meta, dict):
                meta = {}
            if "token_cap" in meta:
                continue

            merged = {**meta, "token_cap": default, "token_window_days": 7}
            try:
                resp = await self._request(
                    "POST",
                    "team/update",
                    json={"team_id": str(team_id), "metadata": merged},
                )
                self._raise_http(resp)
            except BackendUnavailableError:
                _LOGGER.warning("token cap backfill failed for %s", alias)
                continue
            patched.append(alias)

        if patched:
            _LOGGER.info(
                "token cap %s applied to %d team(s) without one: %s",
                f"{default:,}",
                len(patched),
                ", ".join(sorted(patched)),
            )

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
                "metadata": {
                    "grantor": GRANTOR_FREE_TIER,
                    # A project starts with an allocation so the page has
                    # something to show and "Request more" has something to
                    # request against. Deliberately generous for the pilot:
                    # a cap that binds generates support requests and teaches
                    # us nothing about real usage, while one that rarely
                    # binds measures what people actually draw.
                    "token_cap": int(getattr(self._cfg.budgets, "default_team_token_cap", 0) or 0),
                    "token_window_days": 7,
                },
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

    # Runtime tier overrides live on a reserved LiteLLM team rather than in
    # llmao's own state: llmao has no database, and the only thing worse than
    # a missing override is one that disagrees between two instances.
    _TIER_STORE_TEAM = "llmao-entitlement-defaults"

    async def tier_entitlement(self, tier: str) -> dict[str, Any]:
        """A tier's effective limits: config, with any admin override on top.

        Config is the floor and the fallback. An override that has never been
        set leaves config showing through, so a fresh deployment has working
        limits before anyone opens an admin page.
        """
        effective = dict(resolve_entitlement(self._cfg, tier))
        tier_cfg = getattr(getattr(self._cfg, "entitlements", None), tier, None)
        for name in ("token_cap", "token_window_days"):
            val = getattr(tier_cfg, name, None)
            if val is not None:
                effective[name] = val

        try:
            info = await self.team_info(self._TIER_STORE_TEAM)
        except BackendUnavailableError:
            return effective
        if info is None:
            return effective
        stored = (info.metadata or {}).get(tier)
        if isinstance(stored, dict):
            effective.update({k: v for k, v in stored.items() if v is not None})
        return effective

    async def set_tier_entitlement(
        self,
        tier: str,
        *,
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        max_parallel_requests: int | None = None,
        token_cap: int | None = None,
        token_window_days: int | None = None,
    ) -> dict[str, Any]:
        """Change a tier's ceilings for everyone on it. RAI admin only.

        The lever for "the free tier is too generous" or "nobody can get
        anything done": one change, every key on that tier, no redeploy.

        ONE ASYMMETRY WORTH SURFACING IN THE UI. The token cap is re-read on
        every check, so a change takes effect immediately. rpm, tpm and
        max_parallel_requests live on the LiteLLM key object and existing keys
        keep what they were minted with -- an admin who lowers rpm and sees no
        change would reasonably conclude the control is broken. A sweep
        calling update_key_entitlement across a tier would fix that and is not
        built yet.
        """
        tier = (tier or "").strip()
        if not tier:
            raise BackendUnavailableError("set_tier_entitlement requires a tier")

        changes = {
            k: v
            for k, v in (
                ("rpm_limit", rpm_limit),
                ("tpm_limit", tpm_limit),
                ("max_parallel_requests", max_parallel_requests),
                ("token_cap", token_cap),
                ("token_window_days", token_window_days),
            )
            if v is not None
        }
        if not changes:
            raise BackendUnavailableError("set_tier_entitlement: nothing to change")
        for name, val in changes.items():
            if int(val) < 0:
                raise BackendUnavailableError(f"{name} must not be negative")

        team = await self.ensure_team(self._TIER_STORE_TEAM)
        meta = dict(team.metadata or {})
        meta[tier] = {**(meta.get(tier) or {}), **changes}

        resp = await self._request(
            "POST",
            "team/update",
            json={"team_id": team.team_id, "metadata": meta},
        )
        self._raise_http(resp)
        _LOGGER.info("tier %s updated: %s", tier, sorted(changes))
        # The cap is re-read on every check, but cached buckets are not --
        # without this an admin lowering a cap sees a stale remaining figure
        # for up to a minute and concludes the control did not work.
        self.invalidate_allowance()
        return await self.tier_entitlement(tier)

    async def update_team_entitlement(
        self,
        project: str,
        *,
        max_budget_usd: float | None = None,
        token_cap: int | None = None,
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        max_parallel_requests: int | None = None,
    ) -> TeamInfo:
        """Adjust what a project may spend and how fast it may draw.

        Site-admin operation: a project's allocation is a claim on shared
        capacity, so it is not something a PMC can raise for itself.

        Only the named fields are sent. Passing None leaves LiteLLM's current
        value alone rather than clearing it, which matters because a partial
        update that silently zeroed the others would uncap the project.

        max_budget_usd binds only on commercial models. Self-hosted spend
        accrues at a derived price and will never reach a ceiling, so the rate
        limits are what actually constrain a project's draw on the fleet.
        """
        team_id = await self.ensure_team_id(project)
        payload: dict[str, Any] = {"team_id": team_id}
        if max_budget_usd is not None:
            payload["max_budget"] = float(max_budget_usd)
        if token_cap is not None:
            # Capacity lives in metadata, not a LiteLLM field: LiteLLM meters
            # dollars, and merging rather than replacing keeps grantor and
            # anything else already there.
            existing = await self.team_info(project)
            meta = dict(existing.metadata or {}) if existing else {}
            meta["token_cap"] = int(token_cap)
            payload["metadata"] = meta
        for name, val in (
            ("rpm_limit", rpm_limit),
            ("tpm_limit", tpm_limit),
            ("max_parallel_requests", max_parallel_requests),
        ):
            if val is not None:
                payload[name] = int(val)
        if len(payload) == 1:
            raise BackendUnavailableError("update_team_entitlement: nothing to change")

        resp = await self._request("POST", "team/update", json=payload)
        self._raise_http(resp)
        _LOGGER.info("team/update %s %s", project, sorted(payload))
        live = await self.team_info(project)
        if live is None:
            raise BackendUnavailableError(f"team {project} vanished after update")
        return live

    async def update_key_entitlement(
        self,
        token_id: str,
        *,
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        max_parallel_requests: int | None = None,
        max_budget_usd: float | None = None,
    ) -> None:
        """Adjust one key's ceiling, within its team's allocation.

        PMC operation: distributing a project's share among its members, and
        capping a service key so a retry loop cannot take the whole project
        with it.

        LiteLLM does not enforce that the sum of a team's key limits stays
        under the team's own. It cannot -- limits are per-minute and a team
        limit is also per-minute, so N keys at the team rate each is legal and
        merely means they contend. Treat key limits as fairness between
        members, and the team limit as the real ceiling.
        """
        token_id = (token_id or "").strip()
        if not token_id:
            raise BackendUnavailableError("update_key_entitlement requires token_id")
        payload: dict[str, Any] = {"key": token_id}
        if max_budget_usd is not None:
            payload["max_budget"] = float(max_budget_usd)
        for name, val in (
            ("rpm_limit", rpm_limit),
            ("tpm_limit", tpm_limit),
            ("max_parallel_requests", max_parallel_requests),
        ):
            if val is not None:
                payload[name] = int(val)
        if len(payload) == 1:
            raise BackendUnavailableError("update_key_entitlement: nothing to change")

        resp = await self._request("POST", "key/update", json=payload)
        self._raise_http(resp)
        _LOGGER.info("key/update %s %s", token_id[:12], sorted(payload))

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
        tier: str = TIER_PROJECT,
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
        # Rate limits are the only control on self-hosted capacity: those
        # models have no marginal cost, so their spend accrues at a derived
        # price and no budget can ever be exhausted by them. A key minted
        # without limits can saturate the fleet, and LiteLLM's default is
        # unlimited.
        meta.setdefault("tier", tier)
        limits = resolve_entitlement(self._cfg, tier)

        payload: dict[str, Any] = {
            "team_id": team_id,
            "metadata": meta,
            **limits,
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

    # Spend reads are cached for the same reason allowance reads are: a page
    # refresh should not cost a log scan. Short enough that a running job's
    # draw appears while it is still running.
    _SPEND_TTL_S = 60

    async def usage(self, project: str | None) -> list[dict]:
        """Spend rows for a project, or across all projects when None.

        One row per (user, model) pair over the current week, carrying tokens
        and cost. Callers group it further -- the project page by member, My
        Keys by project -- which is why this returns rows rather than a
        summary: three pages want three groupings of the same query, and
        three queries would be three chances to disagree.

        `cost_basis` is carried through so a caller can tell an estimate from
        an amount payable. Self-hosted routes are tagged derived_capacity by
        deployment_body; commercial rows have no tag and their cost is real.
        """
        rows = await self._spend_rows()
        if project is None:
            return rows
        return [r for r in rows if r.get("project") == project]

    async def key_usage(self, user: str) -> dict[str, int]:
        """Tokens drawn per key this week, for one person.

        Same underlying query as usage(), grouped by key instead. The
        allowance is per person; this is what each key contributed to it.
        """
        out: dict[str, int] = {}
        for r in await self._spend_rows():
            if r.get("user") != user:
                continue
            token = r.get("token_id")
            if token:
                out[token] = out.get(token, 0) + int(r.get("total_tokens") or 0)
        return out

    async def _spend_rows(self) -> list[dict]:
        """This week's spend log, normalised and cached.

        LiteLLM's /spend/logs returns one row per request. A busy week is
        thousands of rows, so they are folded to one per
        (user, project, model, key) before anything else sees them --
        otherwise every caller pays to iterate the raw log.
        """
        now = time.monotonic()
        if self._spend_cache is not None:
            at, rows = self._spend_cache
            if (now - at) < self._SPEND_TTL_S:
                return rows

        start = _dt.datetime.now(_dt.UTC).date() - _dt.timedelta(days=6)
        resp = await self._request(
            "GET",
            "spend/logs",
            params={"start_date": start.isoformat()},
        )
        self._raise_http(resp)
        raw = resp.json()
        if isinstance(raw, dict):
            raw = raw.get("data") or raw.get("logs") or []
        if not isinstance(raw, list):
            raw = []

        by_key: dict[tuple, dict] = {}
        for row in raw:
            if not isinstance(row, dict):
                continue
            meta = row.get("metadata") or {}
            if not isinstance(meta, dict):
                meta = {}
            # user_id is the request's principal; metadata.project is set by
            # llmao when the key is minted. A row with neither is a call made
            # with a key llmao did not issue, which is worth keeping rather
            # than dropping -- it still consumed the fleet.
            user = row.get("user") or row.get("user_id")
            project = meta.get("project") or row.get("team_alias")
            model = row.get("model")
            token = row.get("api_key") or row.get("token")

            total = row.get("total_tokens")
            if total is None:
                total = (row.get("prompt_tokens") or 0) + (row.get("completion_tokens") or 0)

            k = (user, project, model, token)
            acc = by_key.get(k)
            if acc is None:
                acc = by_key[k] = {
                    "user": user,
                    "project": project,
                    "model": model,
                    "token_id": token,
                    "total_tokens": 0,
                    "total_cost_usd": 0.0,
                    "cost_basis": self._cost_basis.get(model),
                    "requests": 0,
                }
            try:
                acc["total_tokens"] += int(total or 0)
                acc["total_cost_usd"] += float(row.get("spend") or 0.0)
                acc["requests"] += 1
            except (TypeError, ValueError):
                continue

        rows = list(by_key.values())
        self._spend_cache = (now, rows)
        return rows

    def invalidate_spend(self) -> None:
        """Drop the cached spend rows. Paired with invalidate_allowance."""
        self._spend_cache = None

    # Allowance reads are cached: a committer refreshing a page should not
    # cost a spend-log scan each time. Short enough that a long job's draw
    # shows up while it is still running.
    _ALLOWANCE_TTL_S = 60

    async def allowance(self, user: str, *, tier: str = TIER_FREE) -> RollingAllowance:
        """A principal's rolling allowance, derived from the spend log.

        NOT stored. The buckets are computed from LiteLLM's own spend rows on
        read, which has two properties worth the query cost:

        The allowance cannot drift from actual usage, because it IS actual
        usage. A stored counter can disagree with the log after a crash, a
        double-count or a replay, and the disagreement is invisible until
        somebody audits it.

        And there is nothing to migrate when this is replaced. The design
        this is standing in for -- defer a free-tier call when projects need
        the box -- needs no allowance at all, so a stored one would be a
        table to drop and a UI to unwind. Derived, it just stops being
        called.

        The cost is a query per principal per TTL. On a pilot-sized spend log
        that is cheap; at scale it wants either LiteLLM's daily aggregate
        table or a materialised counter, and the shape here does not change
        if that happens.
        """
        user = (user or "").strip()
        if not user:
            raise BackendUnavailableError("allowance requires a user")

        limits = await self.tier_entitlement(tier)
        cap = int(limits.get("token_cap") or 0)
        window = int(limits.get("token_window_days") or 7)
        if cap <= 0:
            # No cap configured for this tier: an allowance with no ceiling
            # is not meaningful, so say so rather than returning one that
            # silently never exhausts.
            raise BackendUnavailableError(f"tier {tier} has no token_cap; nothing to meter against")

        cached = self._allowance_cache.get(user)
        if cached is not None:
            at, rows = cached
            if (time.monotonic() - at) < self._ALLOWANCE_TTL_S:
                return RollingAllowance(cap=cap, window_days=window, buckets=dict(rows))

        buckets = await self._spend_buckets(user, window)
        self._allowance_cache[user] = (time.monotonic(), dict(buckets))
        return RollingAllowance(cap=cap, window_days=window, buckets=buckets)

    async def _spend_buckets(self, user: str, window_days: int) -> dict[str, int]:
        """Daily token totals for one user over the window.

        Counts TOTAL tokens -- prompt plus completion -- because the
        allowance is a share of the fleet and both halves consume it. Prefill
        is cheaper per token than decode, which is why the DOLLAR figures are
        weighted and this one is not: an allowance is a quota, not a bill.
        """
        start = _dt.datetime.now(_dt.UTC).date() - _dt.timedelta(days=window_days - 1)
        resp = await self._request(
            "GET",
            "spend/logs",
            params={"user_id": user, "start_date": start.isoformat()},
        )
        self._raise_http(resp)
        rows = resp.json()
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("logs") or []
        if not isinstance(rows, list):
            return {}

        buckets: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            stamp = row.get("startTime") or row.get("start_time")
            if not stamp:
                continue
            day = str(stamp)[:10]
            total = row.get("total_tokens")
            if total is None:
                total = (row.get("prompt_tokens") or 0) + (row.get("completion_tokens") or 0)
            try:
                buckets[day] = buckets.get(day, 0) + int(total)
            except (TypeError, ValueError):
                continue
        return buckets

    def invalidate_allowance(self, user: str | None = None) -> None:
        """Drop cached allowance rows.

        Called after a tier change, since the cap is re-read on every check
        but the cached buckets are not. Without this an admin lowering a cap
        sees the old remaining figure for up to a minute, which looks like
        the control not working.
        """
        if user is None:
            self._allowance_cache.clear()
        else:
            self._allowance_cache.pop(user, None)

    def deployment_body(self, dep) -> dict:
        """POST /model/new payload from models.yaml + this deployment's api_base."""
        entry = self.fleet.models.get(dep.model_name)
        if entry is None:
            raise BackendUnavailableError(f"models.yaml missing {dep.model_name}; cannot POST /model/new")
        params = dict(entry.litellm_params)
        if not dep.api_base:
            raise BackendUnavailableError(f"{dep.name}: no api_base for /model/new")
        params["api_base"] = dep.api_base
        if dep.self_hosted and dep.vllm is not None and dep.vllm.api_key:
            params["api_key"] = dep.vllm.api_key
        info: dict[str, Any] = {
            "self_hosted": dep.self_hosted,
            "asf_api_base": _norm_base(dep.api_base),
        }

        # Price self-hosted tokens so usage reporting shows something other
        # than 0.00. LiteLLM has no cost-map entry for these models, so every
        # completion prices at nothing and a project's draw is invisible.
        #
        # The price is derived per model in models.yaml from measured
        # throughput: hourly rental over tokens per second, input and output
        # separately because prefill runs at thousands per second and decode
        # at tens. On this fleet that is a 12-27x ratio, close to what
        # commercial providers charge.
        #
        # It follows the card rather than being one fleet-wide figure, because
        # a faster box costs more per hour and should cost more per token --
        # otherwise a model's price stops tracking what serving it costs.
        #
        # IT IS NOT A MARGINAL COST. The boxes are rented continuously, so a
        # token costs the same whether or not anyone asks for it, and the
        # figure above assumes a box decoding flat out. At 25% utilisation the
        # real cost is four times this; at 5%, twenty times. Read it as a unit
        # of fair share that happens to carry a dollar sign -- useful for
        # spotting a runaway job and comparing one project's draw against
        # another's, and wrong the moment anyone treats it as money the
        # Foundation could have kept.
        #
        # The UI shows it alongside a token count so the distinction survives
        # contact with a reader.
        if dep.self_hosted:
            pricing = getattr(entry, "pricing", None)
            if pricing is not None:
                for name in ("input_cost_per_token", "output_cost_per_token"):
                    val = getattr(pricing, name, None)
                    if val is not None:
                        info[name] = float(val)
                # Marks the row as capacity rather than money, so a report can
                # keep the two apart instead of adding them together.
                info["cost_basis"] = "derived_capacity"
                # Remembered so a spend row can be classified without a
                # second call to /model/info per page load.
                self._cost_basis[dep.model_name] = "derived_capacity"

        return {
            "model_name": dep.model_name,
            "litellm_params": params,
            "model_info": info,
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
        """POST /model/new for this deployment. No serving check.

        Callers decide the gate. sync_selfhost waits until vLLM is SERVING.
        POST /do-add-deployment uses add_refusal for the same gate, then calls
        this. A 400 or 409 means LiteLLM already has the row.
        """
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
        """At startup: POST /model/new for each commercial models.yaml deployment."""
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
            if srv.state == HEALTHY and dep.api_base and not dep.in_litellm:
                await self.add_deployment(dep)
            elif srv.state in (UNHEALTHY, STALLED) and dep.in_litellm:
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
        routes = _routes_from_model_info(resp.json())
        by_base: dict[str, list[str]] = {}
        for norm, _host, _port, model_name in routes:
            by_base.setdefault(norm, []).append(model_name)
        intended = {_norm_base(d.api_base) for d in self.fleet.deployments if d.api_base}
        now = time.time()
        for dep in self.fleet.deployments:
            base = _norm_base(dep.api_base) if dep.api_base else None
            names = by_base.get(base, []) if base else []
            dep.in_litellm = bool(names)
            dep.config_mismatch = any(name != dep.model_name for name in names)
            self._store_skew(dep, now)
        extra: list[ExtraLiteLLM] = []
        for norm, host, port, model_name in routes:
            if norm in intended:
                continue
            view = deployment_status(
                DeploymentSnapshot(model_name=model_name, self_hosted=False, litellm_only=True),
                self._cfg.fleet,
                now,
            )
            extra.append(ExtraLiteLLM(host=host, port=port, model_name=model_name, skew=view.skew))
        self.fleet.extra_litellm = extra
        if extra:
            _LOGGER.warning(
                "skew: %s %s",
                SKEW_PHRASE[SKEW_IN_LITELLM_NOT_IN_CONFIG],
                [(row.host, row.port, row.model_name) for row in extra],
            )

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
            self._store_skew(dep, stamp)

    def _store_skew(self, dep, now: float) -> None:
        """Replace stored notes with the badges model_status computes now."""
        view = deployment_status(snapshot_for_status(dep, self._cfg.fleet, now), self._cfg.fleet, now)
        fresh = [badge for badge in view.skew if badge not in dep.skew]
        dep.skew = list(view.skew)
        for badge in fresh:
            _LOGGER.warning("skew: %s@%s %s", dep.name, dep.api_base, SKEW_PHRASE[badge])


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


def _routes_from_model_info(body: Any) -> list[tuple[str, str, int | None, str]]:
    """Each LiteLLM row as (normalised api_base, host, port, model_name).

    Host and port stay separate. One machine serves several ports, and one
    host:port can still report more than one model name.
    """
    rows = []
    if isinstance(body, list):
        rows = body
    elif isinstance(body, dict):
        rows = body.get("data") or body.get("models") or []
    out: list[tuple[str, str, int | None, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        params = row.get("litellm_params") or {}
        if not isinstance(params, dict) or not params.get("api_base"):
            continue
        norm = _norm_base(params["api_base"])
        parsed = urlparse(norm)
        out.append((norm, parsed.hostname or "", parsed.port, str(row.get("model_name") or "")))
    return out

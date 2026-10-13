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

import datetime as _dt
import json
import logging
import pathlib
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from asfpy.stopwatch import Stopwatch
from easydict import EasyDict as edict  # noqa: N813

from llmao import access_requests as _access
from llmao.fleet import ACCESS_GROUP_SELF_HOSTED
from llmao.proxy.deployments import DeploymentManager
from llmao.proxy.errors import BackendUnavailableError
from llmao.token_window import RollingAllowance

_LOGGER = logging.getLogger(__name__)


# Who authorized the project's dollar ceiling. First cfg default → Free Tier.
GRANTOR_FREE_TIER = "Free Tier"

# Key tiers. The name is stored in key metadata so a later audit can tell what
# a key was minted as, and so /key/update can re-apply the right ceiling.
TIER_FREE = "free_tier"
TIER_PROJECT = "project"
TIER_SERVICE = "service"
TIERS = (TIER_FREE, TIER_PROJECT, TIER_SERVICE)

# A key that belongs to no project still has to be in a LiteLLM team, so
# there are two pretend projects for them, one per kind of projectless key.
# Being teams, they give those keys what a real project's keys have: usage
# that can be told apart, and one place to say which models they may use.
#
# The names are scoped the way llmao's rows in the memory store are. An LDAP
# project name has no colon in it, so these can never collide with one.
PROJECT_FREE_TIER = "llmao:project:free-tier"
PROJECT_SERVICE = "llmao:project:service"

# What a page calls each of them. The names above are never shown.
PRETEND_PROJECTS = {
    PROJECT_FREE_TIER: "free tier",
    PROJECT_SERVICE: "service, no project",
}


def pretend_project(tier: str) -> str:
    """Where a key with no project goes, by its tier."""
    return PROJECT_FREE_TIER if tier == TIER_FREE else PROJECT_SERVICE


# LiteLLM fields these map to. Named here so the mapping is in one place
# rather than spread across call sites.
_LIMIT_FIELDS = ("rpm_limit", "tpm_limit", "max_parallel_requests")

# The dependency line is `litellm[proxy,extra-proxy]==1.102.2`. The extras are optional.
_LITELLM_PIN = re.compile(r"""litellm(?:\[[^\]]+\])?==([^"'\s]+)""")


def pinned_litellm_version() -> str:
    """The litellm version pyproject.toml requires. The admin page compares the proxy to this."""
    text = (pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    match = _LITELLM_PIN.search(text)
    if match is None:
        raise RuntimeError("pyproject.toml has no litellm pin")
    return match.group(1)


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


class LiteLLMTransport:
    """The HTTP side of talking to LiteLLM's admin API, as the master key.

    Everything that asks LiteLLM for something goes through one of these:
    it holds the connection and the timeout, times each call, and turns the
    ways a call can fail into the one error the pages know how to show.

    It is handed to whatever needs it rather than built there, so a test can
    hand over a stand-in (tests/fake_transport.py) and exercise the real
    code above it without a proxy.
    """

    def __init__(self, cfg: Any, *, client: httpx.AsyncClient | None = None):
        self._cfg = cfg
        base = cfg.litellm.base_url.rstrip("/") + "/"
        timeout_s = int(cfg.litellm.request_timeout_s)
        self._client = client or httpx.AsyncClient(
            base_url=base,
            headers={
                "Authorization": f"Bearer {cfg.litellm.master_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout_s),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One call. An answer of any status is returned; see raise_http."""
        try:
            # One per call, built in the `with`: these overlap, and a shared
            # Stopwatch would time one request from another's start.
            with Stopwatch(name=f"LiteLLM {method} {path}"):
                resp = await self._client.request(method, path, **kwargs)
            return resp
        except httpx.TimeoutException as e:
            raise BackendUnavailableError(
                f"LiteLLM admin API timed out after {self._cfg.litellm.request_timeout_s}s"
            ) from e
        except httpx.ConnectError as e:
            raise BackendUnavailableError(f"could not reach LiteLLM at {self._cfg.litellm.base_url}") from e

    def raise_http(self, resp: httpx.Response) -> None:
        """Turn an error status into BackendUnavailableError.

        Separate from request() because some callers look first: a 404 from
        the memory store is an answer, not a failure.
        """
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise BackendUnavailableError(f"LiteLLM admin error {e.response.status_code}: {e.response.text}") from e


class LiteLLMBackend:
    """Everything llmao asks of a running litellm proxy, as one object.

    It builds the managers, each over the one transport, and holds them;
    callers go to the manager for what they want -- `backend.keys.find()`,
    `backend.teams.info()`. What each manager keeps in memory is its own
    business and is described there. Call ``warm()`` at process start
    (fail-fast).
    """

    def __init__(self, cfg: Any, fleet: Any, *, transport: Any = None):
        self._transport = transport or LiteLLMTransport(cfg)
        self.deployments = DeploymentManager(cfg, fleet, self._transport)
        memory = MemoryStore(self._transport)
        self.requests = AccessRequestManager(memory)
        self.skew = SkewManager(memory)
        self.log = LogManager(memory)
        self.tiers = TierManager(cfg, memory)
        self.teams = TeamManager(cfg, self._transport)
        self.metering = UsageManager(self._transport, fleet, self.teams, self.tiers)
        self.keys = KeyManager(self._transport, self.teams, self.tiers)

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def proxy_version(self) -> str | None:
        """litellm_version from GET /health/readiness/details, or None when it cannot be read.

        On 1.102.2 that field is only on the authenticated details route.
        GET /health probes every model, so it is not used here.
        """
        try:
            resp = await self._transport.request("GET", "health/readiness/details")
            self._transport.raise_http(resp)
            body = resp.json()
        except BackendUnavailableError:
            return None
        version = body.get("litellm_version") if isinstance(body, dict) else None
        return str(version) if version else None

    async def warm(self) -> None:
        """Load project→team_id from LiteLLM. Fail-fast if the proxy is unreachable."""
        rows = await self.teams.rows(fresh=True)
        await self.teams.backfill_token_caps(rows)
        await self.teams.ensure_pretend_projects()
        await self.deployments.ensure_commercial()
        # Six of the week's seven days are history and are never asked for
        # again, so reading them here means no visitor waits for them. Not
        # worth refusing to start over: a cold cache is only slow.
        try:
            await self.metering.warm()
        except BackendUnavailableError as e:
            _LOGGER.warning("could not warm usage: %s", e)


class KeyManager:
    """LiteLLM's virtual keys: listing, minting, revoking, and what each may do.

    Handed the transport, the teams -- every key is minted into its
    project's team -- and the tiers, which say what a new key may do.
    """

    def __init__(self, transport: Any, teams: Any, tiers: Any):
        self._transport = transport
        self.teams = teams
        self.tiers = tiers

    async def update_entitlement(
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

        resp = await self._transport.request("POST", "key/update", json=payload)
        self._transport.raise_http(resp)
        _LOGGER.info("key/update %s %s", token_id[:12], sorted(payload))

    async def find(
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
            team_id = await self.teams.team_id(project)
            if not team_id:
                return []
            params["team_id"] = team_id
        resp = await self._transport.request("GET", "key/list", params=params)
        self._transport.raise_http(resp)
        body = resp.json()
        keys = body.get("keys") if isinstance(body, dict) else body
        if not keys:
            return []
        try:
            return [_normalize_key_obj(k) for k in keys]
        except ValueError as e:
            raise BackendUnavailableError(str(e)) from e

    async def create(
        self,
        *,
        project: str,
        purpose: str,
        user: str | None = None,
        tier: str,
        metadata: dict | None = None,
        token_cap: int | None = None,
    ) -> CreatedKey:
        """Mint a key on `tier`, which decides its rate limits.

        The tier has no default. It used to be the project tier, and a
        caller that forgot to say got project limits for a pipeline key.
        """
        project = (project or "").strip()
        if not project:
            raise BackendUnavailableError("create_key requires project")
        team = await self.teams.ensure(project)
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
        meta["tier"] = tier
        if token_cap is not None:
            # A per-key allowance, set by an admin when approving. Only a
            # projectless service key really needs one: a project key draws
            # on the project's share and a personal key on the person's, but
            # a key belonging to neither has nothing to draw on, and
            # unattended automation would consume a week in an afternoon.
            meta["token_cap"] = int(token_cap)
        # From the tiers rather than from config alone: an admin who has
        # changed a tier's limits is told they apply to keys issued from
        # then on, and this is where that is made true.
        limits = await self.tiers.key_limits(tier)

        payload: dict[str, Any] = {
            "team_id": team_id,
            "metadata": meta,
            **limits,
            # key_alias is globally unique in LiteLLM — do not put purpose there.
        }
        if user:
            payload["user_id"] = user
        resp = await self._transport.request("POST", "key/generate", json=payload)
        self._transport.raise_http(resp)
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

    async def delete(self, token_id: str) -> None:
        resp = await self._transport.request("POST", "key/delete", json={"keys": [token_id]})
        self._transport.raise_http(resp)


class UsageManager:
    """Who used what: this week's tokens and cost, and each person's allowance.

    Handed the transport, and the three things it has to ask: the fleet (is
    this model's cost an estimate?), the teams (whose project is this?) and
    the tiers (what is the allowance?).
    """

    def __init__(self, transport: Any, fleet: Any, teams: Any, tiers: Any):
        self._transport = transport
        self.fleet = fleet
        self.teams = teams
        self.tiers = tiers
        # ISO date → usage rows for that UTC day, from LiteLLM's daily
        # aggregate. Spend and allowances are both derived from this.
        self._daily: dict[str, list[dict]] = {}
        # Days that can no longer change: kept, never refetched.
        self._closed_days: set[str] = set()
        # Monotonic stamp of the last read of the days still open.
        self._daily_at: float | None = None
        # The longest window anyone has asked for; older days are dropped.
        self._daily_span = 0

    async def warm(self) -> None:
        """Read the week now, so that no visitor waits for the six days that are history."""
        await self._daily_rows(self._SPEND_DAYS)

    # Usage is read from LiteLLM's daily aggregate, never from the raw log.
    #
    # /spend/logs was the first choice and the wrong one. It filters by date
    # only when BOTH start_date and end_date are sent; given one, it ignores
    # it and returns the 10,000 most recent requests. That took twenty
    # seconds on the pilot fleet and was not "this week" at all -- it reached
    # back further on a quiet week and would have fallen short on a busy one.
    #
    # /user/daily/activity is one row per (day, model, key), a second for the
    # whole fleet's week, and it carries each key's owner and team.
    #
    # A day that has ended is history: nothing will be added to it, so it is
    # read once and kept. Only the days still open are refetched, and those
    # are cached for a minute -- short enough that a running job's draw
    # appears while it is still running.
    _SPEND_TTL_S = 60
    _SPEND_DAYS = 7

    # LiteLLM files a request under the day it STARTED, and writes the
    # aggregate from a background queue. So a day goes on changing a little
    # after midnight, until the last request begun in it has finished and
    # been flushed. An hour is generous for both.
    _DAY_CLOSE_GRACE_S = 3600

    _DAILY_PAGE_SIZE = 1000

    async def rows(self, project: str | None) -> list[dict]:
        """Spend rows for a project, or across all projects when None.

        One row per (user, model) pair over the current week, carrying tokens
        and cost. Callers group it further -- the project page by member, My
        Keys by project -- which is why this returns rows rather than a
        summary: three pages want three groupings of the same query, and
        three queries would be three chances to disagree.

        `cost_basis` is carried through so a caller can tell an estimate from
        an amount payable. It comes from the fleet: a model served from our
        own boxes is derived_capacity; anything else has no tag and its cost
        is real.
        """
        rows = await self._spend_rows()
        if project is None:
            return rows
        return [r for r in rows if r.get("project") == project]

    async def by_key(self, user: str) -> dict[str, int]:
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
        """This week's usage, one row per (user, project, model, key).

        The daily rows carry a date as well; it is folded away here so that
        no caller pays to add seven days together.
        """
        daily = await self._daily_rows(self._SPEND_DAYS)

        # Rows name a team by id; every page groups by project. Resolved here
        # rather than when the day is stored, so a day that closed before its
        # team was known is not left without a project for good.
        seen = {r["team_id"] for rows in daily.values() for r in rows if r["team_id"]}
        project_of = await self.teams.projects(seen)

        by_key: dict[tuple, dict] = {}
        for rows in daily.values():
            for row in rows:
                project = project_of.get(row["team_id"])
                k = (row["user"], project, row["model"], row["token_id"])
                acc = by_key.get(k)
                if acc is None:
                    acc = by_key[k] = {
                        "user": row["user"],
                        "project": project,
                        "model": row["model"],
                        "token_id": row["token_id"],
                        "total_tokens": 0,
                        "total_cost_usd": 0.0,
                        "cost_basis": self.fleet.cost_basis(row["model"]) if self.fleet else None,
                        "requests": 0,
                    }
                acc["total_tokens"] += row["total_tokens"]
                acc["total_cost_usd"] += row["total_cost_usd"]
                acc["requests"] += row["requests"]
        return list(by_key.values())

    async def _daily_rows(self, days: int) -> dict[str, list[dict]]:
        """Usage rows for each of the last `days` UTC days, today included.

        Closed days come from memory. The fetch covers the earliest day still
        open through today: only today in the steady state, yesterday as well
        for the first hour after midnight, the whole window once at startup.

        No coordination between callers. Two that find the open days stale
        both ask, and the later answer is kept.
        """
        now = self._now()
        today = now.date()
        wanted = [(today - _dt.timedelta(days=i)).isoformat() for i in range(days)]
        self._daily_span = max(self._daily_span, days)

        open_days = [d for d in wanted if d not in self._closed_days]
        fresh = self._daily_at is not None and (time.monotonic() - self._daily_at) < self._SPEND_TTL_S
        if not fresh or any(d not in self._daily for d in wanted):
            start = _dt.date.fromisoformat(min(open_days))
            fetched = await self._fetch_daily(start, today)
            grace = _dt.timedelta(seconds=self._DAY_CLOSE_GRACE_S)
            day = start
            while day <= today:
                iso = day.isoformat()
                # A day with no traffic is absent from the answer, and that
                # absence is the answer: store it empty rather than not at all.
                self._daily[iso] = fetched.get(iso, [])
                day += _dt.timedelta(days=1)
                ended = _dt.datetime.combine(day, _dt.time(), tzinfo=_dt.UTC)
                if now >= ended + grace:
                    self._closed_days.add(iso)
            self._daily_at = time.monotonic()

            oldest = (today - _dt.timedelta(days=self._daily_span - 1)).isoformat()
            for iso in [d for d in self._daily if d < oldest]:
                del self._daily[iso]
                self._closed_days.discard(iso)

        return {d: self._daily.get(d, []) for d in wanted}

    def _now(self) -> _dt.datetime:
        return _dt.datetime.now(_dt.UTC)

    async def _fetch_daily(self, start: _dt.date, end: _dt.date) -> dict[str, list[dict]]:
        """One read of LiteLLM's daily aggregate, as rows grouped by date.

        The answer nests day -> model group -> key. The model GROUP is the
        route name llmao registered, which is the name the fleet knows it
        by; the `models` breakdown beside it names the provider's model.
        """
        out: dict[str, list[dict]] = {}
        page = 1
        while True:
            resp = await self._transport.request(
                "GET",
                "user/daily/activity",
                params={
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "page": page,
                    "page_size": self._DAILY_PAGE_SIZE,
                },
            )
            self._transport.raise_http(resp)
            body = resp.json()
            if not isinstance(body, dict):
                break
            for result in body.get("results") or []:
                if not isinstance(result, dict) or not result.get("date"):
                    continue
                rows = out.setdefault(str(result["date"])[:10], [])
                groups = (result.get("breakdown") or {}).get("model_groups") or {}
                for model, group in groups.items():
                    keys = group.get("api_key_breakdown") if isinstance(group, dict) else None
                    if not isinstance(keys, dict):
                        continue
                    for token, entry in keys.items():
                        if not isinstance(entry, dict):
                            continue
                        metrics = entry.get("metrics") or {}
                        meta = entry.get("metadata") or {}
                        total = metrics.get("total_tokens")
                        if total is None:
                            total = (metrics.get("prompt_tokens") or 0) + (metrics.get("completion_tokens") or 0)
                        try:
                            row = {
                                # The key's owner and team. A key llmao did
                                # not issue has neither, and is kept rather
                                # than dropped -- it still consumed the fleet.
                                "user": meta.get("user_id"),
                                "team_id": str(meta["team_id"]) if meta.get("team_id") else None,
                                "model": model,
                                "token_id": token,
                                "total_tokens": int(total or 0),
                                "total_cost_usd": float(metrics.get("spend") or 0.0),
                                "requests": int(metrics.get("api_requests") or 0),
                            }
                        except (TypeError, ValueError):
                            continue
                        rows.append(row)
            if not (body.get("metadata") or {}).get("has_more"):
                break
            page += 1
        return out

    def invalidate(self) -> None:
        """Have the next read ask again. Closed days are history and stay."""
        self._daily_at = None

    async def allowance(self, user: str, *, tier: str = TIER_FREE) -> RollingAllowance:
        """A principal's rolling allowance, derived from recorded usage.

        NOT stored. The buckets are computed from LiteLLM's own daily usage
        on read, which has two properties worth the query cost:

        The allowance cannot drift from actual usage, because it IS actual
        usage. A stored counter can disagree with the log after a crash, a
        double-count or a replay, and the disagreement is invisible until
        somebody audits it.

        And there is nothing to migrate when this is replaced. The design
        this is standing in for -- defer a free-tier call when projects need
        the box -- needs no allowance at all, so a stored one would be a
        table to drop and a UI to unwind. Derived, it just stops being
        called.

        It costs no query of its own: the buckets are this person's share of
        the same daily rows the spend pages read.

        Counts TOTAL tokens -- prompt plus completion -- because the
        allowance is a share of the fleet and both halves consume it. Prefill
        is cheaper per token than decode, which is why the DOLLAR figures are
        weighted and this one is not: an allowance is a quota, not a bill.
        """
        user = (user or "").strip()
        if not user:
            raise BackendUnavailableError("allowance requires a user")

        limits = await self.tiers.limits(tier)
        cap = int(limits.get("token_cap") or 0)
        window = int(limits.get("token_window_days") or 7)
        if cap <= 0:
            # No cap configured for this tier: an allowance with no ceiling
            # is not meaningful, so say so rather than returning one that
            # silently never exhausts.
            raise BackendUnavailableError(f"tier {tier} has no token_cap; nothing to meter against")

        # Only what was drawn through the person's free-tier keys, which are
        # the ones in the free-tier pretend project. What they draw through
        # a project's key comes out of that project's share, and counting it
        # here as well would fill their bar with work the allowance was
        # never meant to cover.
        free = await self.teams.team_id(PROJECT_FREE_TIER)
        buckets: dict[str, int] = {}
        for day, rows in (await self._daily_rows(window)).items():
            spent = sum(r["total_tokens"] for r in rows if r["team_id"] == free and r["user"] == user)
            if spent:
                buckets[day] = spent
        return RollingAllowance(cap=cap, window_days=window, buckets=buckets)


class TeamManager:
    """LiteLLM's teams, one per project: which is which, and what each may spend.

    Handed the config and the transport. It keeps the listing for a minute,
    and the project -> team id map for good: an id never changes, and teams
    are only ever added.
    """

    def __init__(self, cfg: Any, transport: Any):
        self._cfg = cfg
        self._transport = transport
        self._team_ids: dict[str, str] = {}  # project → team_id
        # (monotonic stamp, team/list rows).
        self._team_rows_cache: tuple[float, list[dict[str, Any]]] | None = None

    def __len__(self) -> int:
        """How many projects have a team that we know of."""
        return len(self._team_ids)

    async def team_id(self, project: str) -> str | None:
        """The project's team id, or None if it has no team. Looks once if unknown."""
        if project not in self._team_ids:
            await self.rows()
        return self._team_ids.get(project)

    async def projects(self, team_ids) -> dict[str, str]:
        """{team id: project}, for telling whose usage a row is.

        A team made since the last listing will not be known yet, so if any
        of `team_ids` is a stranger the listing is consulted first.
        """
        if set(team_ids) - set(self._team_ids.values()):
            await self.rows()
        return {tid: project for project, tid in self._team_ids.items()}

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

    # The listing is every team with its budget and metadata, and it is the
    # slowest admin call left. Sixty seconds: long enough that a page load
    # is one listing rather than several, short enough that a cap an admin
    # has just changed shows while they are still looking at the page.
    _TEAM_TTL_S = 60

    async def rows(self, *, fresh: bool = False) -> list[dict[str, Any]]:
        if not fresh and self._team_rows_cache is not None:
            at, rows = self._team_rows_cache
            if (time.monotonic() - at) < self._TEAM_TTL_S:
                return rows
        rows = await self._team_list_rows_uncached()
        self._team_rows_cache = (time.monotonic(), rows)
        return rows

    async def _team_list_rows_uncached(self) -> list[dict[str, Any]]:
        resp = await self._transport.request("GET", "team/list")
        self._transport.raise_http(resp)
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

    async def backfill_token_caps(self, rows: list[dict[str, Any]]) -> None:
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
                resp = await self._transport.request(
                    "POST",
                    "team/update",
                    json={"team_id": str(team_id), "metadata": merged},
                )
                self._transport.raise_http(resp)
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

    # What a pretend project's keys may use: our own boxes, and nothing
    # else. Commercial access needs an owner who can be asked about the
    # bill, and a PMC is that owner; a key with no project has none.
    #
    # LiteLLM enforces this, not llmao -- requests go to it directly, and a
    # rule kept only in our pages would stop nothing. A team with a `models`
    # list may use those and no others, and every key in the team is bound
    # by it. A real project's team has no list, which LiteLLM reads as any.
    _PRETEND_MODELS = frozenset({ACCESS_GROUP_SELF_HOSTED})

    async def ensure_pretend_projects(self) -> None:
        """Make the pretend projects' teams, and see that they are restricted.

        Done at startup rather than when the first projectless key is
        minted, so that they always exist and always have a team id: the
        allowance is counted by one, and both say which models a projectless
        key may use.

        A team that is already there is checked. One that allows anything
        other than what it should is put right, and that is logged as an
        error: until this ran, a free key could reach a model that costs
        money.
        """
        listed = {row.get("team_alias"): row for row in await self.rows()}
        for project in PRETEND_PROJECTS:
            row = listed.get(project)
            if row is None:
                await self.ensure(project)
            elif set(row.get("models") or []) != self._PRETEND_MODELS:
                _LOGGER.error(
                    "%s allowed %s, not %s; restricting it",
                    project,
                    sorted(row.get("models") or []) or "any model",
                    sorted(self._PRETEND_MODELS),
                )
                resp = await self._transport.request(
                    "POST",
                    "team/update",
                    json={"team_id": row["team_id"], "models": sorted(self._PRETEND_MODELS)},
                )
                self._transport.raise_http(resp)
                self._team_rows_cache = None

    async def ensure_id(self, project: str) -> str:
        """Map LDAP project (team_alias) → LiteLLM team_id; create team if needed."""
        info = await self.ensure(project)
        return info.team_id

    async def ensure(self, project: str) -> TeamInfo:
        """Ensure a LiteLLM team exists for the project; return live budget facts."""
        project = (project or "").strip()
        if not project:
            raise BackendUnavailableError("project is required")

        existing = await self.info(project)
        if existing is not None:
            return existing

        budget_usd = float(self._cfg.budgets.default_team_budget_usd)
        duration = resolve_budget_duration(None, self._cfg)
        new_team = {
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
        }
        if project in PRETEND_PROJECTS:
            # No dollar ceiling. A real project's budget is that project's
            # to exhaust; this one would be shared by everybody with a
            # projectless key, and one person reaching it would stop them all.
            del new_team["max_budget"], new_team["budget_duration"]
            new_team["models"] = sorted(self._PRETEND_MODELS)
        resp = await self._transport.request("POST", "team/new", json=new_team)
        if resp.status_code >= 400:
            again = await self.info(project)
            if again is not None:
                return again
            self._transport.raise_http(resp)

        body = resp.json() if resp.content else {}
        if not isinstance(body, dict):
            body = {}
        team_id = body.get("team_id")
        if not team_id:
            raise BackendUnavailableError("LiteLLM team/new returned no team_id")
        self._team_ids[project] = str(team_id)
        live = await self.info(project)
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

    async def update_entitlement(
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
        team_id = await self.ensure_id(project)
        payload: dict[str, Any] = {"team_id": team_id}
        if max_budget_usd is not None:
            payload["max_budget"] = float(max_budget_usd)
        if token_cap is not None:
            # Capacity lives in metadata, not a LiteLLM field: LiteLLM meters
            # dollars, and merging rather than replacing keeps grantor and
            # anything else already there.
            existing = await self.info(project)
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

        resp = await self._transport.request("POST", "team/update", json=payload)
        self._transport.raise_http(resp)
        _LOGGER.info("team/update %s %s", project, sorted(payload))
        live = await self.info(project)
        if live is None:
            raise BackendUnavailableError(f"team {project} vanished after update")
        return live

    async def info(self, project: str) -> TeamInfo | None:
        """Live team spend/budget. Cache holds ids only; spend is never cached.

        Cache hit → team/info. Cache miss → team/list (fills cache + returns
        list-row fields; no second team/info).
        """
        project = (project or "").strip()
        if not project:
            return None

        if project in self._team_ids:
            team_id = self._team_ids[project]
            resp = await self._transport.request(
                "GET",
                "team/info",
                params={"team_id": team_id},
            )
            self._transport.raise_http(resp)
            body = resp.json()
            info = body.get("team_info") or body
            if not isinstance(info, dict):
                info = {}
            return self._team_info_from_dict(info, team_id=team_id)

        rows = await self.rows()
        for row in rows:
            if str(row.get("team_alias") or "") == project:
                return self._team_info_from_dict(row)
        return None


class TierManager:
    """What each tier of key is allowed: config, and what an admin has changed.

    Handed the config and the MemoryStore. The changes are one row there,
    every tier in it.
    """

    _TIERS_KEY = "llmao:tiers"

    def __init__(self, cfg: Any, memory: MemoryStore):
        self._cfg = cfg
        self._memory = memory

    async def _overrides(self) -> dict[str, Any]:
        """{tier: {limit: value}} as an admin has set them. Empty until one has."""
        try:
            held = json.loads(await self._memory.get(self._TIERS_KEY) or "{}")
        except ValueError:
            return {}
        return held if isinstance(held, dict) else {}

    async def limits(self, tier: str) -> dict[str, Any]:
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
            stored = (await self._overrides()).get(tier)
        except BackendUnavailableError:
            return effective
        if isinstance(stored, dict):
            effective.update({k: v for k, v in stored.items() if v is not None})
        return effective

    async def key_limits(self, tier: str) -> dict[str, Any]:
        """The rate limits a key minted on this tier is given.

        Config with the admin's changes on top, like limits(), but only what
        LiteLLM holds on a key. The token cap is not one of those: it is
        metered here, against usage, and LiteLLM would not know the field.
        """
        return {k: v for k, v in (await self.limits(tier)).items() if k in _LIMIT_FIELDS}

    async def set_limits(
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

        # Every tier shares the one row, so this is read, change, write.
        tiers = await self._overrides()
        tiers[tier] = {**(tiers.get(tier) or {}), **changes}
        await self._memory.put(self._TIERS_KEY, json.dumps(tiers, sort_keys=True))
        _LOGGER.info("tier %s updated: %s", tier, sorted(changes))
        return await self.limits(tier)


class AccessRequestManager:
    """Where access requests are kept: one MemoryStore row each.

    Handed the store. The requests themselves -- what one is, and what may
    be done to it -- are llmao/access_requests.py; this only keeps them.
    """

    def __init__(self, memory: MemoryStore):
        self._memory = memory

    async def add(self, req: Any) -> Any:
        """Store a new request, or persist a decision on an existing one.

        One row per request, so answering one cannot lose another.
        """
        await self._memory.put(_access.key_for(req.id), _access.dumps(req))

        # Nothing else trims the store, so do it while writing: a lapsed
        # approval, or a decision older than the history we keep, goes. The
        # request is already saved; failing to tidy is no reason to say it
        # was not.
        try:
            stored = await self.find_all()
            kept = _access.prune(stored)
            for old in stored:
                if old not in kept:
                    await self._memory.delete(_access.key_for(old.id))
        except BackendUnavailableError as e:
            _LOGGER.warning("could not trim stored requests: %s", e)
        return req

    # Same operation; named for what the caller means.
    save = add

    async def find_all(self, project: str | None = None) -> list[Any]:
        """Requests for one project, or every one."""
        out = []
        for row in await self._memory.find(_access.KEY_PREFIX):
            req = _access.loads(row.get("value"))
            if req is not None and (project is None or req.project == project):
                out.append(req)
        return out

    async def find(self, request_id: str) -> Any | None:
        return _access.loads(await self._memory.get(_access.key_for(request_id)))


def _notice(row: dict[str, Any]) -> edict | None:
    """A stored skew or log row as the pages want it, or None if it is not one."""
    try:
        held = json.loads(row.get("value") or "")
    except ValueError:
        return None
    if not isinstance(held, dict):
        return None
    # LiteLLM stamps every row itself, so when a thing was first and last
    # seen does not have to be kept by us.
    return edict(held, key=row.get("key"), first_seen=row.get("created_at"), last_seen=row.get("updated_at"))


class SkewManager:
    """Work that needs doing: something is out of line and a person must act.

    One row per thing that is wrong, keyed `llmao:skew:<kind>:<subject>`. A
    row is there for as long as the thing is wrong and goes when it is put
    right -- never because somebody has looked at it. Whatever checks for a
    kind of skew owns that kind's rows: it says what it found, and the rows
    are made to match.

    Handed the MemoryStore.
    """

    PREFIX = "llmao:skew:"

    def __init__(self, memory: MemoryStore):
        self._memory = memory

    def _key(self, kind: str, subject: str) -> str:
        return f"{self.PREFIX}{kind}:{subject}"

    async def open(self, kind: str | None = None) -> list[edict]:
        """Everything still wrong, or everything of one kind."""
        prefix = self._key(kind, "") if kind else self.PREFIX
        return [n for n in map(_notice, await self._memory.find(prefix)) if n is not None]

    async def reconcile(self, kind: str, found: dict[str, dict[str, Any]]) -> None:
        """Make the rows of one kind match what a check has just found.

        `found` is {subject: what is wrong with it}. A subject not seen
        before gets a row, one whose trouble has changed is rewritten, and a
        row for anything no longer in `found` is removed: it has been fixed.
        A row that is already right is left alone, so a problem that sits
        for a week is not rewritten on every pass.
        """
        held = {n.subject: n for n in await self.open(kind)}
        for subject, detail in found.items():
            row = {"kind": kind, "subject": subject, **detail}
            old = held.get(subject) or {}
            # what is stored, without the three fields _notice adds on the way out
            stored = {k: v for k, v in old.items() if k not in ("key", "first_seen", "last_seen")}
            if stored != row:
                await self._memory.put(self._key(kind, subject), json.dumps(row, sort_keys=True))
        for subject in held.keys() - found.keys():
            await self._memory.delete(self._key(kind, subject))

    async def clear(self, kind: str, subject: str) -> None:
        """One thing has just been put right; do not wait for the next check."""
        await self._memory.delete(self._key(kind, subject))


class LogManager:
    """Things somebody should see: it has happened, and they ought to know.

    Unlike skew there is nothing to do about an entry but read it, so it
    goes when it has been acknowledged. This is not an audit trail -- it is
    for what needs attention, and an entry is gone once it has had it.

    An entry is for the site admins, or for one person: why their key
    stopped working is better told to them than found out by a support
    ticket. The store can only be asked for keys by prefix, so the audience
    is the start of the key: `llmao:log:admin:` or `llmao:log:user:<uid>:`.

    Handed the MemoryStore.
    """

    PREFIX = "llmao:log:"

    def __init__(self, memory: MemoryStore):
        self._memory = memory

    def _audience(self, user: str | None) -> str:
        return f"{self.PREFIX}user:{user}:" if user else f"{self.PREFIX}admin:"

    async def note(self, what: str, *, user: str | None = None, subject: str | None = None, **detail: Any) -> None:
        """Record something for the admins, or for `user`.

        With a `subject` there is one entry for it however often it happens,
        carrying a count: the same thing found on every pass of a check is
        one line to read, not hundreds. Without one, each call is its own
        entry.
        """
        key = self._audience(user) + (subject or secrets.token_urlsafe(9))
        count = 1
        if subject:
            try:
                count = int(json.loads(await self._memory.get(key) or "{}").get("count") or 0) + 1
            except (ValueError, AttributeError):
                pass
        await self._memory.put(key, json.dumps({"what": what, "count": count, **detail}, sort_keys=True))

    async def unseen(self, user: str | None = None) -> list[edict]:
        """What the admins have not acknowledged, or what `user` has not."""
        return [n for n in map(_notice, await self._memory.find(self._audience(user))) if n is not None]

    def is_for(self, key: str, user: str | None = None) -> bool:
        """Whether an entry's key belongs to this audience. For checking an acknowledgement."""
        return key.startswith(self._audience(user))

    async def ack(self, key: str) -> None:
        """Seen. The entry is removed."""
        await self._memory.delete(key)


class MemoryStore:
    """LiteLLM's /v1/memory: a key, a string value, one row each.

    Handed the transport. Whatever keeps its records here is handed this.
    """

    # llmao has no database of its own, and ARCHITECTURE.md states that as a
    # property. What it needs to remember -- requests awaiting an answer, and
    # the tier limits an admin has changed -- goes in LiteLLM's /v1/memory: a
    # key, a string value, one row each.
    #
    # Both used to live in team metadata, which made listing requests a read
    # of every team and every write a rewrite of a team's whole blob.
    #
    # Nothing here is cached. There are a handful of rows and a handful of
    # people; asking each time is quick, and leaves nothing to go stale.

    def __init__(self, transport: Any):
        self._transport = transport

    async def get(self, key: str) -> str | None:
        resp = await self._transport.request("GET", f"v1/memory/{key}")
        if resp.status_code == 404:
            return None
        self._transport.raise_http(resp)
        body = resp.json()
        return body.get("value") if isinstance(body, dict) else None

    async def put(self, key: str, value: str) -> None:
        resp = await self._transport.request("PUT", f"v1/memory/{key}", json={"value": value})
        self._transport.raise_http(resp)

    async def delete(self, key: str) -> None:
        resp = await self._transport.request("DELETE", f"v1/memory/{key}")
        if resp.status_code != 404:
            self._transport.raise_http(resp)

    async def find(self, prefix: str) -> list[dict[str, Any]]:
        """Every row whose key starts with `prefix`, in one page.

        Outstanding requests are counted on one hand, and add_request trims
        the decided ones, so a page is far more than there will ever be.
        """
        resp = await self._transport.request("GET", "v1/memory", params={"key_prefix": prefix, "page_size": 500})
        self._transport.raise_http(resp)
        body = resp.json()
        rows = body.get("memories") if isinstance(body, dict) else None
        return [r for r in rows or [] if isinstance(r, dict)]

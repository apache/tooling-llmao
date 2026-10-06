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

"""The seam — ASF identity joined to LiteLLM teams and PATs.

Project names are LDAP/session names. PATs are project + user + purpose
(design §5.1); automation keys omit user (team-scoped exception).

The seam speaks **project** only; mapping to LiteLLM team_id is the backend's job.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Any

from . import requests as _requests
from .litellm_client import (
    GRANTOR_FREE_TIER,
    TIER_FREE,
    TIER_PROJECT,
    TIER_SERVICE,
    Backend,
    BackendUnavailableError,
    CreatedKey,
    KeyInfo,
    TeamInfo,
)


class AuthzError(Exception):
    """Caller is not allowed to perform this action on the project."""


def require_member(fn):
    """Expects (self, identity, project, ...)."""

    @functools.wraps(fn)
    async def wrapper(self, identity: Identity, project: str, *args, **kwargs):
        if not (identity.is_site_admin or identity.member_of(project)):
            raise AuthzError(f"{identity.uid} is not a member of {project}")
        return await fn(self, identity, project, *args, **kwargs)

    return wrapper


def require_admin(fn):
    """Expects (self, identity, project, ...)."""

    @functools.wraps(fn)
    async def wrapper(self, identity: Identity, project: str, *args, **kwargs):
        if not identity.admin_of(project):
            raise AuthzError(f"{identity.uid} is not a PMC admin of {project}")
        return await fn(self, identity, project, *args, **kwargs)

    return wrapper


@dataclass
class Identity:
    """The subset of an asfquart ClientSession the seam needs."""

    uid: str
    projects: list[str]  # committer projects (LDAP names)
    committees: list[str]  # PMC memberships (admin within those projects)
    is_site_admin: bool = False

    def member_of(self, project: str) -> bool:
        return project in self.projects or project in self.committees

    def admin_of(self, project: str) -> bool:
        return self.is_site_admin or project in self.committees

    def all_projects(self) -> list[str]:
        return list(dict.fromkeys([*self.committees, *self.projects]))


@dataclass
class ProjectListRow:
    """One project on the Projects list (budget summary)."""

    project: str
    is_steward: bool
    max_budget: float
    spend: float
    remaining: float
    pct_used: float | None
    budget_duration: str
    grantor: str = GRANTOR_FREE_TIER


@dataclass
class PersonSpendRow:
    uid: str
    spend: float
    key_count: int


@dataclass
class AutomationKeyRow:
    """Secret-free automation key summary for project overview."""

    token_id: str
    purpose: str
    spend: float
    created_by: str | None
    blocked: bool


@dataclass
class ProjectOverview:
    """Read-only project budget + people/automation usage (key spend aggregates)."""

    project: str
    is_steward: bool
    max_budget: float
    spend: float
    remaining: float
    pct_used: float | None
    budget_duration: str
    grantor: str
    people_spend: float
    automation_spend: float
    by_person: list[PersonSpendRow] = field(default_factory=list)
    automation_keys: list[AutomationKeyRow] = field(default_factory=list)
    automation_key_count: int = 0


def _pct_used(spend: float, max_budget: float) -> float | None:
    if max_budget <= 0:
        return None
    return round(100.0 * spend / max_budget, 2)


def _remaining(spend: float, max_budget: float) -> float:
    return round(max(0.0, max_budget - spend), 6)


def _aggregate_keys(
    keys: list[KeyInfo],
) -> tuple[float, float, list[PersonSpendRow], list[AutomationKeyRow]]:
    people = 0.0
    automation = 0.0
    by_uid: dict[str, list] = {}
    auto_rows: list[AutomationKeyRow] = []
    for k in keys:
        if k.is_automation:
            automation += k.spend
            auto_rows.append(
                AutomationKeyRow(
                    token_id=k.token_id,
                    purpose=k.purpose or "",
                    spend=k.spend,
                    created_by=k.created_by,
                    blocked=k.blocked,
                )
            )
        else:
            people += k.spend
            uid = k.user or ""
            if uid not in by_uid:
                by_uid[uid] = [0.0, 0]
            by_uid[uid][0] += k.spend
            by_uid[uid][1] += 1
    by_person = [PersonSpendRow(uid=uid, spend=round(vals[0], 6), key_count=vals[1]) for uid, vals in by_uid.items()]
    by_person.sort(key=lambda r: (-r.spend, r.uid))
    auto_rows.sort(key=lambda r: (-r.spend, r.token_id))
    return round(people, 6), round(automation, 6), by_person, auto_rows


def _tokens_short(n) -> str:
    """340k reads faster than 340,000, and these are glanced at."""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


class Seam:
    def __init__(self, cfg: Any, backend: Backend):
        self._cfg = cfg
        self._backend = backend

    def _row_from_team(self, project: str, identity: Identity, info: TeamInfo) -> ProjectListRow:
        return ProjectListRow(
            project=project,
            is_steward=identity.admin_of(project),
            max_budget=info.max_budget,
            spend=info.spend,
            remaining=_remaining(info.spend, info.max_budget),
            pct_used=_pct_used(info.spend, info.max_budget),
            budget_duration=info.budget_duration,
            grantor=info.grantor,
        )

    async def list_projects_for(self, identity: Identity) -> list[ProjectListRow]:
        """Projects from identity membership; ensures LiteLLM team (project budget)."""
        names = sorted(identity.all_projects())
        rows: list[ProjectListRow] = []
        for project in names:
            info = await self._backend.ensure_team(project)
            rows.append(self._row_from_team(project, identity, info))
        return rows

    @require_member
    async def project_overview(self, identity: Identity, project: str) -> ProjectOverview:
        """Read-only project budget + key-based people/automation breakdown."""
        project = (project or "").strip()
        info = await self._backend.ensure_team(project)
        keys = await self._backend.list_keys(project=project, size=100)
        people, automation, by_person, auto_keys = _aggregate_keys(keys)
        return ProjectOverview(
            project=project,
            is_steward=identity.admin_of(project),
            max_budget=info.max_budget,
            spend=info.spend,
            remaining=_remaining(info.spend, info.max_budget),
            pct_used=_pct_used(info.spend, info.max_budget),
            budget_duration=info.budget_duration,
            grantor=info.grantor,
            people_spend=people,
            automation_spend=automation,
            by_person=by_person,
            automation_keys=auto_keys,
            automation_key_count=len(auto_keys),
        )

    @require_member
    async def team_status(self, identity: Identity, project: str) -> TeamInfo | None:
        return await self._backend.team_info(project)

    # ---- requests -------------------------------------------------------

    @require_member
    async def request_capacity(self, identity: Identity, project: str, *, token_cap: int, reason: str):
        """A PMC asks for a larger weekly allocation.

        Member-gated, not admin-gated: asking is not a privilege. Granting
        is, and that lives on the other side.
        """
        if token_cap <= 0:
            raise AuthzError("Requested allocation must be a positive number of tokens.")
        if not (reason or "").strip():
            # The approver's first question is why. Asking for it here saves
            # a round trip and makes the queue readable.
            raise AuthzError("Say what the extra capacity is for.")
        req = _requests.Request.new(
            kind=_requests.KIND_CAPACITY,
            requester=identity.uid,
            project=project,
            reason=reason,
            wanted={"token_cap": int(token_cap)},
        )
        return await self._backend.add_request(req)

    async def request_key(
        self,
        identity: Identity,
        *,
        purpose: str,
        tier: str,
        reason: str,
        project: str | None = None,
    ):
        """Someone asks for a key outside a project, or for automation.

        No project gate: a projectless key belongs to nobody but the asker.
        A key FOR a project still goes through the admin queue, because the
        thing being approved is an allowance on shared capacity.
        """
        purpose = (purpose or "").strip()
        if not purpose:
            raise AuthzError("A key needs a purpose so it can be told from your others.")
        if not (reason or "").strip():
            raise AuthzError("Say what the key is for.")
        if project and project not in (identity.committees or []):
            raise AuthzError(f"You are not on the {project} PMC.")
        req = _requests.Request.new(
            kind=_requests.KIND_KEY,
            requester=identity.uid,
            project=project,
            reason=reason,
            wanted={"purpose": purpose, "tier": tier or TIER_SERVICE},
        )
        return await self._backend.add_request(req)

    @require_member
    async def project_requests(self, identity: Identity, project: str) -> list:
        """Requests against one project, newest first."""
        reqs = await self._backend.list_requests(project)
        return sorted(reqs, key=lambda r: r.created_at or "", reverse=True)

    async def tiers(self, identity: Identity) -> list[dict]:
        """Every tier's effective limits: config, with any override applied."""
        if not identity.is_site_admin:
            raise AuthzError("Tiers are limited to site admins.")
        out = []
        for tier in (TIER_FREE, TIER_PROJECT, TIER_SERVICE):
            out.append({"tier": tier, **(await self._backend.tier_entitlement(tier))})
        return out

    async def set_tier(self, identity: Identity, tier: str, **limits) -> dict:
        """Change a tier's ceilings for everyone on it.

        The lever for "the free tier is too generous" or "nobody can get
        anything done" -- one change, every key on that tier, no redeploy.
        """
        if not identity.is_site_admin:
            raise AuthzError("Changing tiers is limited to site admins.")
        named = {k: v for k, v in limits.items() if v is not None}
        if not named:
            raise AuthzError("Nothing to change.")
        return await self._backend.set_tier_entitlement(tier, **named)

    async def my_requests(self, identity: Identity) -> list:
        """This person's own requests, newest first.

        Shown where they asked, so an answer finds them without an email.
        """
        mine = [r for r in await self._backend.list_requests() if r.requester == identity.uid]
        return sorted(mine, key=lambda r: r.created_at or "", reverse=True)

    async def pending_requests(self, identity: Identity) -> list:
        """Everything awaiting a decision, oldest first.

        Oldest first because the queue is a to-do list: the request that has
        been waiting three days is the one someone is wondering about.
        """
        if not identity.is_site_admin:
            raise AuthzError("The request queue is limited to site admins.")
        pending = [r for r in await self._backend.list_requests() if r.state == _requests.STATE_PENDING]
        return sorted(pending, key=lambda r: r.created_at or "")

    async def decide_request(
        self,
        identity: Identity,
        request_id: str,
        *,
        approve: bool,
        reason: str = "",
        granted: dict | None = None,
    ):
        """Approve, adjust or deny. Capacity approvals apply immediately.

        A capacity bump takes effect on approval rather than waiting for the
        PMC to do anything -- there is nothing for them to exercise, and a
        grant that needs a second step is a grant that sits unapplied.
        """
        req = await self._backend.find_request(request_id)
        if req is None:
            raise AuthzError("No such request.")
        req.decide(
            state=_requests.STATE_APPROVED if approve else _requests.STATE_DENIED,
            by=identity.uid,
            reason=reason,
            granted=granted,
        )
        if approve and req.kind == _requests.KIND_CAPACITY:
            await self._backend.update_team_entitlement(req.project, token_cap=int(req.granted.get("token_cap") or 0))
        return await self._backend.save_request(req)

    @require_member
    async def project_usage(self, identity: Identity, project: str) -> list[dict]:
        """Per-principal draw for one project, service keys separated out.

        Member-gated: a project's internal breakdown is not public, and the
        decorator is the same one every other project read uses.

        A service key becomes its own row rather than being folded into
        whoever created it. Two reasons: the member figures must sum to the
        project total or the page reads as broken, and a pipeline is often
        the largest single consumer -- which is the thing a PMC most needs to
        see and the thing hiding it inside a person's row would obscure.
        """
        rows = await self._backend.usage(project)
        keys = await self._backend.list_keys(project=project, size=200)
        # token_id -> (display name, is_service). A key with no user is
        # automation; one with a user belongs to that person.
        owner: dict[str, tuple[str, bool]] = {}
        key_count: dict[str, int] = {}
        for k in keys:
            is_service = not k.user or k.is_automation
            name = (k.purpose or k.token_id[:8]) if is_service else k.user
            owner[k.token_id] = (name, is_service)
            key_count[name] = key_count.get(name, 0) + 1

        acc: dict[str, dict] = {}
        for r in rows:
            token = r.get("token_id")
            name, is_service = owner.get(token, (r.get("user") or "unknown", False))
            e = acc.setdefault(
                name,
                {
                    "name": name,
                    "is_service": is_service,
                    "tokens": 0,
                    "spend": 0.0,
                    "keys": key_count.get(name, 0),
                },
            )
            e["tokens"] += int(r.get("total_tokens") or 0)
            # Only commercial rows carry real money. A capacity estimate in a
            # column headed dollars is how the two get added together later.
            if r.get("cost_basis") != "derived_capacity":
                e["spend"] += float(r.get("total_cost_usd") or 0.0)

        # Largest first: the row a PMC is looking for is the one at the top.
        return sorted(acc.values(), key=lambda e: -e["tokens"])

    async def list_my_keys(self, identity: Identity) -> list[KeyInfo]:
        return await self._backend.list_keys(user=identity.uid, size=100)

    async def my_key_usage(self, identity: Identity) -> dict[str, int]:
        """Tokens drawn per key, this week.

        The allowance above is per person; this is what each key contributed
        to it. Separate calls because they answer different questions and the
        spend log groups differently for each.
        """
        try:
            return await self._backend.key_usage(identity.uid)
        except BackendUnavailableError:
            # An empty map renders dashes rather than zeros. "0" claims the
            # key is idle; we only know we could not ask.
            return {}

    async def my_key_approvals(self, identity: Identity) -> list[dict]:
        """Approved key requests this person can still exercise.

        Expired and consumed approvals are filtered here rather than in the
        template, so the page cannot offer a button that would fail.
        """
        return [
            {
                "id": r.id,
                "purpose": r.granted.get("purpose") or r.wanted.get("purpose") or "",
                "tier": r.granted.get("tier") or r.wanted.get("tier") or TIER_SERVICE,
                "project": r.project or "",
                "token_cap": int(r.granted.get("token_cap") or 0),
                "expires_at": r.expires_at(),
            }
            for r in await self._backend.list_requests()
            if r.requester == identity.uid and r.is_actionable()
        ]

    async def create_approved_key(self, identity: Identity, request_id: str) -> CreatedKey:
        """Exercise an approval: create the key, consume the approval.

        An admin approved the RIGHT to create a key with fixed parameters.
        The requester creates it and sees the secret once, so it never passes
        through the admin -- there is no moment where a credential sits in a
        message somewhere.

        Four things are checked rather than trusted to the UI, because this
        takes a request id from a form post: the approval belongs to the
        caller, it is approved, it has not been used, and it has not lapsed.

        The key is created with the APPROVED parameters, never with anything
        the form carries. A form that could name its own tier would make the
        approval decorative.
        """
        req = await self._backend.find_request(request_id)
        if req is None or req.requester != identity.uid:
            # Same message either way: whether a request exists is not
            # something a stranger should be able to probe for.
            raise AuthzError("No such approval.")
        if not req.is_actionable():
            if req.consumed_at:
                raise AuthzError("That approval has already been used.")
            if req.is_expired():
                raise AuthzError("That approval has expired. Please request again.")
            raise AuthzError("That request has not been approved.")

        granted = {**req.wanted, **req.granted}
        created = await self._backend.create_key(
            project=req.project or self._backend._TIER_STORE_TEAM,
            purpose=granted.get("purpose") or "",
            user=identity.uid,
            tier=granted.get("tier") or TIER_SERVICE,
            token_cap=int(granted.get("token_cap") or 0) or None,
        )

        # Consumed AFTER the key exists. The other order burns the approval
        # on a failed creation and leaves the person with nothing and no way
        # to retry.
        req.consume()
        await self._backend.save_request(req)
        return created

    async def my_allowance(self, identity: Identity):
        """This committer's rolling free-tier allowance.

        No authz decision: a principal always sees their own draw. Project
        figures go through the normal membership checks below.
        """
        return await self._backend.allowance(identity.uid)

    async def my_project_usage(self, identity: Identity) -> list[dict]:
        """Per-project draw for the projects this committer belongs to.

        Tokens and dollars are kept as separate fields deliberately. Tokens on
        self-hosted models are a share of capacity the Foundation has already
        paid for; dollars on commercial models are money that did not exist a
        second ago. Summing them would produce a figure with no meaning, and a
        single column invites exactly that.
        """
        out = []
        for project in sorted(identity.committees):
            try:
                rows = await self._backend.usage(project)
            except BackendUnavailableError:
                continue
            tokens = 0
            spend = 0.0
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                if row.get("user") not in (None, identity.uid):
                    continue
                tokens += int(row.get("total_tokens") or 0)
                # Only commercial rows carry real money. Self-hosted spend is
                # priced from measured throughput for reporting, and adding it
                # here would put capacity into a column labelled dollars.
                if row.get("cost_basis") != "derived_capacity":
                    spend += float(row.get("total_cost_usd") or 0.0)
            # The project's own ceilings, so a personal figure has context.
            # "1.2M" says nothing about whether to start a big job; "1.2M of
            # 20M" does.
            cap = budget = None
            try:
                info = await self._backend.team_info(project)
                if info is not None:
                    cap = getattr(info, "token_cap", None)
                    budget = info.max_budget
            except BackendUnavailableError:
                pass

            out.append(
                {
                    "name": project,
                    "tokens_h": _tokens_short(tokens),
                    "cap_h": _tokens_short(cap) if cap else "—",
                    "spend_h": f"${spend:,.2f}",
                    "budget_h": f"${budget:,.0f}" if budget else "—",
                }
            )
        return out

    @require_admin
    async def list_automation_keys(self, identity: Identity, project: str) -> list[KeyInfo]:
        """Automation keys for a project (admin / PMC only)."""
        keys = await self._backend.list_keys(project=project, size=100)
        return [k for k in keys if k.is_automation]

    @require_member
    async def create_personal_key(
        self,
        identity: Identity,
        project: str,
        purpose: str,
    ) -> CreatedKey:
        purpose = (purpose or "").strip()
        return await self._backend.create_key(
            project=project,
            purpose=purpose,
            user=identity.uid,
        )

    @require_admin
    async def create_automation_key(
        self,
        identity: Identity,
        project: str,
        purpose: str,
    ) -> CreatedKey:
        """Admin-only team-scoped key (no user) for scripts after formal request."""
        purpose = (purpose or "").strip()
        return await self._backend.create_key(
            project=project,
            purpose=purpose,
            user=None,
            metadata={"created_by": identity.uid},
        )

    async def revoke_key(self, identity: Identity, token_id: str) -> None:
        token_id = (token_id or "").strip()
        if not token_id:
            raise AuthzError("key id required")
        # Confirm ownership: personal key belongs to uid, or automation key on
        # a project they admin.
        keys = await self._backend.list_keys(size=100)
        match = next((k for k in keys if k.token_id == token_id), None)
        if match is None:
            mine = await self._backend.list_keys(user=identity.uid, size=100)
            match = next((k for k in mine if k.token_id == token_id), None)
        if match is None:
            raise AuthzError("key not found or not visible")
        if match.user == identity.uid:
            await self._backend.delete_key(token_id)
            return
        if match.is_automation:
            project = match.project
            if identity.is_site_admin:
                await self._backend.delete_key(token_id)
                return
            if project and identity.admin_of(project):
                await self._backend.delete_key(token_id)
                return
        raise AuthzError("not allowed to revoke this key")

    @require_admin
    async def project_activity(self, identity: Identity, project: str) -> list[dict]:
        return await self._backend.usage(project)

"""The seam — ASF identity joined to LiteLLM teams and PATs.

Project names are LDAP/session names. PATs are project + user + purpose
(design §5.1); automation keys omit user (team-scoped exception).

The seam speaks **project** only; mapping to LiteLLM team_id is the backend's job.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .litellm_client import (
    GRANTOR_FREE_TIER,
    Backend,
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
    projects: List[str]      # committer projects (LDAP names)
    committees: List[str]    # PMC memberships (admin within those projects)
    is_site_admin: bool = False

    def member_of(self, project: str) -> bool:
        return project in self.projects or project in self.committees

    def admin_of(self, project: str) -> bool:
        return self.is_site_admin or project in self.committees

    def all_projects(self) -> List[str]:
        return list(dict.fromkeys([*self.committees, *self.projects]))


@dataclass
class ProjectListRow:
    """One project on the Projects list (budget summary)."""
    project: str
    is_steward: bool
    max_budget: float
    spend: float
    remaining: float
    pct_used: Optional[float]
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
    created_by: Optional[str]
    blocked: bool


@dataclass
class ProjectOverview:
    """Read-only project budget + people/automation usage (key spend aggregates)."""
    project: str
    is_steward: bool
    max_budget: float
    spend: float
    remaining: float
    pct_used: Optional[float]
    budget_duration: str
    grantor: str
    people_spend: float
    automation_spend: float
    by_person: List[PersonSpendRow] = field(default_factory=list)
    automation_keys: List[AutomationKeyRow] = field(default_factory=list)
    automation_key_count: int = 0


def _pct_used(spend: float, max_budget: float) -> Optional[float]:
    if max_budget <= 0:
        return None
    return round(100.0 * spend / max_budget, 2)


def _remaining(spend: float, max_budget: float) -> float:
    return round(max(0.0, max_budget - spend), 6)


def _aggregate_keys(
    keys: List[KeyInfo],
) -> Tuple[float, float, List[PersonSpendRow], List[AutomationKeyRow]]:
    people = 0.0
    automation = 0.0
    by_uid: Dict[str, list] = {}
    auto_rows: List[AutomationKeyRow] = []
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
    by_person = [
        PersonSpendRow(uid=uid, spend=round(vals[0], 6), key_count=vals[1])
        for uid, vals in by_uid.items()
    ]
    by_person.sort(key=lambda r: (-r.spend, r.uid))
    auto_rows.sort(key=lambda r: (-r.spend, r.token_id))
    return round(people, 6), round(automation, 6), by_person, auto_rows


class Seam:
    def __init__(self, cfg: Any, backend: Backend):
        self._cfg = cfg
        self._backend = backend

    def _row_from_team(
        self, project: str, identity: Identity, info: TeamInfo
    ) -> ProjectListRow:
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

    async def list_projects_for(self, identity: Identity) -> List[ProjectListRow]:
        """Projects from identity membership; ensures LiteLLM team (project budget)."""
        names = sorted(identity.all_projects())
        rows: List[ProjectListRow] = []
        for project in names:
            info = await self._backend.ensure_team(project)
            rows.append(self._row_from_team(project, identity, info))
        return rows

    @require_member
    async def project_overview(
        self, identity: Identity, project: str
    ) -> ProjectOverview:
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
    async def team_status(self, identity: Identity, project: str) -> Optional[TeamInfo]:
        return await self._backend.team_info(project)

    async def list_my_keys(self, identity: Identity) -> List[KeyInfo]:
        return await self._backend.list_keys(user=identity.uid, size=100)

    @require_admin
    async def list_automation_keys(self, identity: Identity, project: str) -> List[KeyInfo]:
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
    async def project_activity(self, identity: Identity, project: str) -> List[Dict]:
        return await self._backend.usage(project)

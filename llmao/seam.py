"""The seam — ASF identity joined to LiteLLM teams and PATs.

Project names are LDAP/session names. PATs are LiteLLM virtual keys
(person = user_id + team_id; automation = team_id only, admin-created).
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .litellm_client import Backend, CreatedKey, KeyInfo, TeamInfo


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


class Seam:
    def __init__(self, cfg: Any, backend: Backend):
        self._cfg = cfg
        self._backend = backend

    async def ensure_project_team(self, project: str) -> TeamInfo:
        return await self._backend.ensure_team(
            project,
            budget_usd=float(self._cfg.budgets.default_team_budget_usd),
            duration=self._cfg.budgets.duration,
        )

    @require_member
    async def team_status(self, identity: Identity, project: str) -> Optional[TeamInfo]:
        return await self._backend.team_info(project)

    async def list_my_keys(self, identity: Identity) -> List[KeyInfo]:
        return await self._backend.list_keys(user_id=identity.uid, size=100)

    @require_admin
    async def list_automation_keys(self, identity: Identity, project: str) -> List[KeyInfo]:
        """Automation keys for a project (admin / PMC only)."""
        team = await self.ensure_project_team(project)
        keys = await self._backend.list_keys(team_id=team.team_id, size=100)
        return [k for k in keys if k.kind == "automation"]

    @require_member
    async def create_personal_key(
        self,
        identity: Identity,
        project: str,
        purpose: str,
    ) -> CreatedKey:
        purpose = (purpose or "").strip()
        if not purpose:
            raise AuthzError("purpose is required")
        team = await self.ensure_project_team(project)
        return await self._backend.create_key(
            team_id=team.team_id,
            key_alias=purpose,
            user_id=identity.uid,
            metadata={"project": project, "kind": "personal"},
        )

    @require_admin
    async def create_automation_key(
        self,
        identity: Identity,
        project: str,
        purpose: str,
    ) -> CreatedKey:
        """Admin-only team-scoped key (no user_id) for scripts after formal request."""
        purpose = (purpose or "").strip()
        if not purpose:
            raise AuthzError("purpose is required for automation keys")
        team = await self.ensure_project_team(project)
        return await self._backend.create_key(
            team_id=team.team_id,
            key_alias=purpose,
            user_id=None,
            metadata={
                "project": project,
                "kind": "automation",
                "created_by": identity.uid,
            },
        )

    async def revoke_key(self, identity: Identity, token: str) -> None:
        token = (token or "").strip()
        if not token:
            raise AuthzError("key id required")
        # Confirm ownership: personal key belongs to uid, or automation key on
        # a project they admin.
        keys = await self._backend.list_keys(size=100)
        match = next((k for k in keys if k.token == token), None)
        if match is None:
            # Try listing as user only
            mine = await self._backend.list_keys(user_id=identity.uid, size=100)
            match = next((k for k in mine if k.token == token), None)
        if match is None:
            raise AuthzError("key not found or not visible")
        if match.user_id == identity.uid:
            await self._backend.delete_key(token)
            return
        if match.kind == "automation":
            project = match.project  # LDAP name from metadata.project
            if identity.is_site_admin:
                await self._backend.delete_key(token)
                return
            if project and identity.admin_of(project):
                await self._backend.delete_key(token)
                return
            # Fall back: any PMC membership that matches team_id via local map
            for p in identity.committees:
                info = await self._backend.team_info(p)
                if info and info.team_id == match.team_id:
                    await self._backend.delete_key(token)
                    return
        raise AuthzError("not allowed to revoke this key")

    @require_admin
    async def project_activity(self, identity: Identity, project: str) -> List[Dict]:
        return await self._backend.usage(project)

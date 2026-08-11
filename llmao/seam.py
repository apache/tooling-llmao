"""The seam — ASF identity joined to LiteLLM teams and PATs.

Project names are LDAP/session names. PATs are project + user + purpose
(design §5.1); automation keys omit user (team-scoped exception).

The seam speaks **project** only; mapping to LiteLLM team_id is the backend's job.
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
        if not purpose:
            raise AuthzError("purpose is required")
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
        if not purpose:
            raise AuthzError("purpose is required for automation keys")
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

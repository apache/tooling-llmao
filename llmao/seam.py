"""The seam — ASF identity joined to LiteLLM teams.

asfquart tells us *who the user is and what projects they're on* (LDAP names).
LiteLLM holds *budgets and virtual keys*. ``cfg`` is ``APP.cfg`` (EasyDict).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .litellm_client import Backend, TeamInfo


class AuthzError(Exception):
    """Caller is not allowed to perform this action on the project."""


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


class Seam:
    def __init__(self, cfg: Any, backend: Backend):
        self._cfg = cfg
        self._backend = backend

    async def ensure_project_team(self, project: str) -> TeamInfo:
        """Resolve (provisioning if needed) the LiteLLM team for an ASF project."""
        return await self._backend.ensure_team(
            project,
            budget_usd=float(self._cfg.budgets.default_team_budget_usd),
            duration=self._cfg.budgets.duration,
        )

    async def team_status(self, project: str) -> Optional[TeamInfo]:
        return await self._backend.team_info(project)

    def require_member(self, identity: Identity, project: str) -> None:
        if not (identity.is_site_admin or identity.member_of(project)):
            raise AuthzError(f"{identity.uid} is not a member of {project}")

    def require_admin(self, identity: Identity, project: str) -> None:
        if not identity.admin_of(project):
            raise AuthzError(f"{identity.uid} is not a PMC admin of {project}")

    async def project_activity(self, identity: Identity, project: str) -> List[Dict]:
        """Everyone's activity in a project. PMC admins (or site admins) only."""
        self.require_admin(identity, project)
        return await self._backend.usage(project)

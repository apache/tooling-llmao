"""The seam — ASF identity joined to litellm teams.

asfquart tells us *who the user is and what projects they're on*. litellm
holds *budgets and virtual keys*. The seam is the join: it resolves an ASF
project to a litellm team (provisioning the team with a budget on first use)
and authorizes that the calling identity may act on that project.

Keeping the ASF-project <-> litellm-team mapping correct as membership changes
is the substance flagged in the plan as "the part that isn't free."
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import Settings
from .litellm_client import Backend, TeamInfo


class AuthzError(Exception):
    """Caller is not allowed to perform this action on the project."""


@dataclass
class Identity:
    """The subset of an asfquart ClientSession the seam needs."""
    uid: str
    projects: List[str]      # committer projects
    committees: List[str]    # PMC memberships (admin within those projects)
    is_site_admin: bool = False

    def member_of(self, project: str) -> bool:
        return project in self.projects or project in self.committees

    def admin_of(self, project: str) -> bool:
        return self.is_site_admin or project in self.committees


class Seam:
    def __init__(self, settings: Settings, backend: Backend):
        self._s = settings
        self._backend = backend

    # -- project / team resolution ----------------------------------------

    def ensure_project_team(self, project: str) -> TeamInfo:
        """Resolve (provisioning if needed) the litellm team for an ASF project."""
        return self._backend.ensure_team(
            project,
            budget_usd=self._s.default_team_budget_usd,
            duration=self._s.budget_duration,
        )

    def team_status(self, project: str) -> Optional[TeamInfo]:
        return self._backend.team_info(project)

    # -- authorization helpers --------------------------------------------

    def require_member(self, identity: Identity, project: str) -> None:
        if not (identity.is_site_admin or identity.member_of(project)):
            raise AuthzError(f"{identity.uid} is not a member of {project}")

    def require_admin(self, identity: Identity, project: str) -> None:
        if not identity.admin_of(project):
            raise AuthzError(f"{identity.uid} is not a PMC admin of {project}")

    # -- activity view ----------------------------------------------------

    def project_activity(self, identity: Identity, project: str) -> List[Dict]:
        """Everyone's activity in a project. PMC admins (or site admins) only."""
        self.require_admin(identity, project)
        return self._backend.usage(project)

"""Authentication / identity resolution via asfquart.

``cfg`` is ``APP.cfg`` (EasyDict). Site admins from ``cfg.site_admins``.
"""
from __future__ import annotations

from typing import Any

import asfquart.session as asf_session

from .seam import AuthzError, Identity


async def current_identity(cfg: Any) -> Identity:
    """Resolve the calling identity from the asfquart session.

    Raises AuthzError if there is no signed-in session (callers sit behind
    ``@asfquart.auth.require``; this is a last-line check, not a login page).
    """
    client_session = await asf_session.read()
    if client_session is None or not getattr(client_session, "uid", None):
        raise AuthzError("not signed in")
    return identity_from_session(client_session, cfg)


def identity_from_session(client_session, cfg: Any) -> Identity:
    site_admins = list(cfg.site_admins or [])
    return Identity(
        uid=client_session.uid,
        projects=list(getattr(client_session, "projects", []) or []),
        committees=list(getattr(client_session, "committees", []) or []),
        is_site_admin=(
            client_session.uid in site_admins
            or bool(getattr(client_session, "isRoot", False))
        ),
    )


def make_token_handler(cfg: Any):
    """asfquart token_handler stub until app-level PATs exist."""
    async def token_handler(token: str):
        return None
    return token_handler

"""Authentication / identity resolution via asfquart.

Reads the real asfquart ``ClientSession`` (oauth.apache.org + LDAP) and maps it
to :class:`~llmao.seam.Identity`. Optional ``token_handler`` support for
bearer tokens against *this* app remains a stub until PAT management lands.
"""
from __future__ import annotations

from typing import Optional

import asfquart.session as asf_session

from .config import Settings
from .seam import Identity


async def current_identity(settings: Settings) -> Optional[Identity]:
    """Resolve the calling identity from the asfquart session (if any)."""
    client_session = await asf_session.read()
    if client_session is None or not getattr(client_session, "uid", None):
        return None
    return identity_from_session(client_session, settings)


def identity_from_session(client_session, settings: Settings) -> Identity:
    return Identity(
        uid=client_session.uid,
        projects=list(getattr(client_session, "projects", []) or []),
        committees=list(getattr(client_session, "committees", []) or []),
        is_site_admin=(
            client_session.uid in settings.site_admins
            or bool(getattr(client_session, "isRoot", False))
        ),
    )


def make_token_handler(settings: Settings):
    """Return an asfquart token_handler that maps a bearer token to a session dict.

    Placeholder until llmao manages PATs for its own API. Returning None means
    "no session" for that token. Shape must match what asfquart expects:
    uid, pmcs, projects, etc.
    """
    async def token_handler(token: str):
        return None
    return token_handler

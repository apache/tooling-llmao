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

"""Authentication / identity resolution via asfquart.

``cfg`` is ``APP.cfg`` (EasyDict). Site admins from ``cfg.site_admins``.

A site admin can choose to operate as a normal user for a while, to see
the site as everyone else does. The choice is a flag in the browser
session, and it takes the admin's standing away everywhere: the identity
handed to the seam is not an admin, so /admin refuses them like anyone
else, not only the navigation. The flag names the uid that set it, so it
is ignored for whoever signs in next on the same browser.
"""

from __future__ import annotations

from typing import Any

import asfquart.session as asf_session
import quart

from .seam import AuthzError, Identity

# quart.session key: the uid of the admin operating as a normal user.
AS_USER = "llmao_as_user"


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
    return Identity(
        uid=client_session.uid,
        projects=list(getattr(client_session, "projects", []) or []),
        committees=list(getattr(client_session, "committees", []) or []),
        is_site_admin=is_site_admin(client_session, cfg) and not acting_as_user(client_session),
    )


def is_site_admin(client_session, cfg: Any) -> bool:
    """Whether this person IS a site admin, whatever they chose to operate as."""
    site_admins = list(cfg.site_admins or [])
    return client_session.uid in site_admins or bool(getattr(client_session, "isRoot", False))


def acting_as_user(client_session) -> bool:
    """Whether this admin chose to operate as a normal user."""
    if not quart.has_request_context():
        return False
    return quart.session.get(AS_USER) == client_session.uid


def act_as_user(client_session, on: bool) -> None:
    if on:
        quart.session[AS_USER] = client_session.uid
    else:
        quart.session.pop(AS_USER, None)


def make_token_handler(cfg: Any):
    """asfquart token_handler stub until app-level PATs exist."""

    async def token_handler(token: str):
        return None

    return token_handler

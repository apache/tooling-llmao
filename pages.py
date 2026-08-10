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

"""HTML pages and static file routes for llmao (STeVe-style)."""

from __future__ import annotations

import pathlib

import asfquart
import asfquart.auth
import asfquart.session
import asfquart.utils
import quart
from easydict import EasyDict as edict

from llmao.auth import current_identity
from llmao.litellm_client import BackendUnavailable, KeyInfo
from llmao.seam import AuthzError

APP = asfquart.APP

THIS_DIR = pathlib.Path(__file__).resolve().parent
TEMPLATES = THIS_DIR / "templates"
STATICDIR = THIS_DIR / "static"


def _render(template_name: str, data) -> str:
    return asfquart.utils.render(APP.load_template(TEMPLATES / template_name), data)


async def basic_info() -> edict:
    """Base-level EZT template data shared by HTML pages."""
    basic = edict()
    basic.title = "llmao"
    basic.flashes = []
    basic.error = None

    # Form defaults for create page
    basic.form_project = ""
    basic.form_purpose = ""
    basic.form_automation = False

    try:
        msgs = quart.get_flashed_messages(with_categories=True)
        basic.flashes = [edict({"category": c, "message": m}) for c, m in msgs]
    except Exception:
        basic.flashes = []

    client_session = await asfquart.session.read()
    if client_session is not None and getattr(client_session, "uid", None):
        basic.uid = client_session.uid
        basic.name = getattr(client_session, "fullname", None) or client_session.uid
        projects = list(
            dict.fromkeys(
                list(getattr(client_session, "committees", None) or [])
                + list(getattr(client_session, "projects", None) or [])
            )
        )
        basic.projects = [edict({"name": p}) for p in projects]
        basic.projects_label = ", ".join(projects) if projects else None
        committees = list(getattr(client_session, "committees", None) or [])
        basic.committees = committees
        site_admins = list(APP.cfg.site_admins or [])
        basic.is_site_admin = (
            client_session.uid in site_admins
            or bool(getattr(client_session, "isRoot", False))
        )
        basic.can_create_automation = basic.is_site_admin or bool(committees)
    else:
        basic.uid = None
        basic.name = None
        basic.projects = []
        basic.projects_label = None
        basic.committees = []
        basic.is_site_admin = False
        basic.can_create_automation = False

    return basic


def _key_rows(keys: list[KeyInfo]) -> list:
    rows = []
    for k in keys:
        project = k.team_alias or k.team_id or "—"
        budget = k.max_budget
        budget_s = f"${budget:.4f}" if budget is not None else "—"
        rows.append(edict({
            "token": k.token,
            "purpose": k.key_alias or "—",
            "project": project,
            "kind": k.kind,
            "kind_label": "Personal" if k.kind == "personal" else "Automation",
            "spend": f"${k.spend:.6f}",
            "max_budget": budget_s,
            "last_used": k.last_used or "—",
            "created_at": k.created_at or "—",
            "blocked": k.blocked,
        }))
    return rows


@APP.get("/")
@APP.use_template(TEMPLATES / "home.ezt")
async def home_page():
    result = await basic_info()
    result.title = "Home"
    return result


@APP.get("/keys")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "keys.ezt")
async def keys_list():
    result = await basic_info()
    result.title = "API keys (PATs)"
    result.keys = []
    result.error = None
    try:
        ident = await current_identity(APP.cfg)
        assert ident is not None
        seam = APP.config["LLMAO_SEAM"]
        by_tok: dict[str, KeyInfo] = {
            k.token: k for k in await seam.list_my_keys(ident)
        }
        admin_projects = (
            ident.all_projects() if ident.is_site_admin else list(ident.committees)
        )
        for p in admin_projects:
            try:
                for k in await seam.list_automation_keys(ident, p):
                    by_tok.setdefault(k.token, k)
            except (AuthzError, BackendUnavailable):
                continue
        result.keys = _key_rows(list(by_tok.values()))
    except (AuthzError, BackendUnavailable) as e:
        result.error = str(e)
    return result


@APP.get("/keys/new")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "key_create.ezt")
async def keys_new_form():
    result = await basic_info()
    result.title = "Create API key"
    return result


@APP.post("/keys/new")
@asfquart.auth.require
async def keys_new_submit():
    form = await quart.request.form
    project = (form.get("project") or "").strip()
    purpose = (form.get("purpose") or "").strip()
    automation = form.get("automation") == "on"

    try:
        ident = await current_identity(APP.cfg)
        assert ident is not None
        seam = APP.config["LLMAO_SEAM"]
        if automation:
            created = await seam.create_automation_key(ident, project, purpose)
        else:
            created = await seam.create_personal_key(ident, project, purpose)
    except (AuthzError, BackendUnavailable) as e:
        result = await basic_info()
        result.title = "Create API key"
        result.error = str(e)
        result.form_project = project
        result.form_purpose = purpose
        result.form_automation = automation
        return _render("key_create.ezt", result)

    result = await basic_info()
    result.title = "API key created"
    result.secret = created.secret
    result.purpose = created.info.key_alias
    result.project = project
    result.kind_label = "Automation" if automation else "Personal"
    return _render("key_created.ezt", result)


@APP.post("/keys/revoke")
@asfquart.auth.require
async def keys_revoke():
    form = await quart.request.form
    token = (form.get("token") or "").strip()
    try:
        ident = await current_identity(APP.cfg)
        assert ident is not None
        seam = APP.config["LLMAO_SEAM"]
        await seam.revoke_key(ident, token)
        await quart.flash("Key revoked.", "success")
    except (AuthzError, BackendUnavailable) as e:
        await quart.flash(str(e), "danger")
    return quart.redirect("/keys")


@APP.get("/static/<path:filename>")
async def serve_static(filename: str):
    return await quart.send_from_directory(STATICDIR, filename)


@APP.get("/favicon.ico")
async def serve_favicon():
    return await quart.send_from_directory(STATICDIR, "favicon.ico")

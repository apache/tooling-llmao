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
from dunamai import Version

from llmao.auth import current_identity
from llmao.litellm_client import BackendUnavailable, KeyInfo
from llmao.models import ux_models
from llmao.seam import AuthzError

APP = asfquart.APP

THIS_DIR = pathlib.Path(__file__).resolve().parent
TEMPLATES = THIS_DIR / "templates"
STATICDIR = THIS_DIR / "static"

REPO_URL = "https://github.com/apache/tooling-llmao"


def _render(template_name: str, data) -> str:
    return asfquart.utils.render(APP.load_template(TEMPLATES / template_name), data)


async def basic_info() -> edict:
    """Base-level EZT template data shared by HTML pages."""
    basic = edict()
    basic.title = "llmao"
    basic.flashes = []
    basic.error = None

    # Form defaults for create pages
    basic.form_project = ""
    basic.form_purpose = ""
    basic.keys_back = "/keys"
    basic.keys_create_another = "/keys/new"

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
        # Other Keys nav + automation mint (provisional: PMC or site admin).
        basic.can_create_automation = basic.is_site_admin or bool(committees)
        if basic.is_site_admin:
            admin_names = projects
        else:
            admin_names = committees
        basic.admin_projects = [edict({"name": p}) for p in admin_names]
    else:
        basic.uid = None
        basic.name = None
        basic.projects = []
        basic.projects_label = None
        basic.committees = []
        basic.is_site_admin = False
        basic.can_create_automation = False
        basic.admin_projects = []

    version = Version.from_git()
    basic.commit = version.commit
    basic.repo = REPO_URL

    return basic


def _key_rows(keys: list[KeyInfo], *, after_path: str = "/keys") -> list:
    rows = []
    for k in keys:
        budget = k.max_budget
        budget_s = f"${budget:.4f}" if budget is not None else "—"
        rows.append(edict({
            "token_id": k.token_id,
            "purpose": k.purpose or "—",
            "project": k.project,
            "kind_label": "Automation" if k.is_automation else "Personal",
            "is_automation": k.is_automation,
            "created_by": k.created_by or "—",
            "spend": f"${k.spend:.6f}",
            "max_budget": budget_s,
            "last_used": k.last_used or "—",
            "created_at": k.created_at or "—",
            "blocked": k.blocked,
            "after_path": after_path,
        }))
    return rows


@APP.get("/")
@APP.use_template(TEMPLATES / "home.ezt")
async def home_page():
    result = await basic_info()
    result.title = "Home"
    return result


@APP.get("/models")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "models.ezt")
async def models_page():
    """Gateway model inventory (public fields; supply path for site admins)."""
    result = await basic_info()
    result.title = "Models"
    result.models = []
    result.error = None
    result.reveal_supply = bool(result.is_site_admin)
    try:
        raw = ux_models(cfg=APP.cfg, reveal_supply=result.reveal_supply)
        result.models = [edict(m) for m in raw]
    except (FileNotFoundError, ValueError, OSError) as e:
        result.error = str(e)
    return result


def _safe_after_path(raw: str | None, default: str = "/keys") -> str:
    """Only allow in-app keys paths as post-revoke/create redirects."""
    if raw in ("/keys", "/keys/other"):
        return raw
    return default


@APP.get("/keys")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "keys.ezt")
async def keys_list():
    """My Keys — personal PATs only (one list_keys call)."""
    result = await basic_info()
    result.title = "My Keys"
    result.keys = []
    result.error = None
    try:
        ident = await current_identity(APP.cfg)
        assert ident is not None
        seam = APP.config["LLMAO_SEAM"]
        my_keys = await seam.list_my_keys(ident)
        result.keys = _key_rows(my_keys, after_path="/keys")
    except (AuthzError, BackendUnavailable) as e:
        result.error = str(e)
    return result


@APP.get("/keys/other")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "keys_other.ezt")
async def keys_other_list():
    """Other Keys — automation / team-scoped (PMC / site admin)."""
    result = await basic_info()
    result.title = "Other Keys"
    result.keys = []
    result.error = None
    if not result.can_create_automation:
        result.error = "Other Keys is limited to PMC members and site admins."
        return result
    try:
        ident = await current_identity(APP.cfg)
        assert ident is not None
        seam = APP.config["LLMAO_SEAM"]
        admin_projects = (
            ident.all_projects() if ident.is_site_admin else list(ident.committees)
        )
        by_id: dict[str, KeyInfo] = {}
        for p in admin_projects:
            try:
                for k in await seam.list_automation_keys(ident, p):
                    by_id.setdefault(k.token_id, k)
            except (AuthzError, BackendUnavailable):
                continue
        result.keys = _key_rows(list(by_id.values()), after_path="/keys/other")
    except (AuthzError, BackendUnavailable) as e:
        result.error = str(e)
    return result


@APP.get("/keys/new")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "key_create.ezt")
async def keys_new_form():
    result = await basic_info()
    result.title = "Create personal key"
    return result


@APP.post("/keys/new")
@asfquart.auth.require
async def keys_new_submit():
    form = await quart.request.form
    project = (form.get("project") or "").strip()
    purpose = (form.get("purpose") or "").strip()

    try:
        ident = await current_identity(APP.cfg)
        assert ident is not None
        seam = APP.config["LLMAO_SEAM"]
        created = await seam.create_personal_key(ident, project, purpose)
    except (AuthzError, BackendUnavailable) as e:
        result = await basic_info()
        result.title = "Create personal key"
        result.error = str(e)
        result.form_project = project
        result.form_purpose = purpose
        return _render("key_create.ezt", result)

    result = await basic_info()
    result.title = "API key created"
    result.secret = created.secret
    result.purpose = created.info.purpose
    result.project = created.info.project
    result.kind_label = "Personal"
    result.is_automation = False
    result.created_by = None
    result.keys_back = "/keys"
    result.keys_create_another = "/keys/new"
    return _render("key_created.ezt", result)


@APP.get("/keys/other/new")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "key_create_other.ezt")
async def keys_other_new_form():
    result = await basic_info()
    result.title = "Create automation key"
    if not result.can_create_automation:
        result.error = "Only PMC members and site admins may create automation keys."
    return result


@APP.post("/keys/other/new")
@asfquart.auth.require
async def keys_other_new_submit():
    form = await quart.request.form
    project = (form.get("project") or "").strip()
    purpose = (form.get("purpose") or "").strip()

    result = await basic_info()
    if not result.can_create_automation:
        result.title = "Create automation key"
        result.error = "Only PMC members and site admins may create automation keys."
        result.form_project = project
        result.form_purpose = purpose
        return _render("key_create_other.ezt", result)

    try:
        ident = await current_identity(APP.cfg)
        assert ident is not None
        seam = APP.config["LLMAO_SEAM"]
        created = await seam.create_automation_key(ident, project, purpose)
    except (AuthzError, BackendUnavailable) as e:
        result.title = "Create automation key"
        result.error = str(e)
        result.form_project = project
        result.form_purpose = purpose
        return _render("key_create_other.ezt", result)

    result.title = "API key created"
    result.secret = created.secret
    result.purpose = created.info.purpose
    result.project = created.info.project
    result.kind_label = "Automation"
    result.is_automation = True
    result.created_by = created.info.created_by
    result.keys_back = "/keys/other"
    result.keys_create_another = "/keys/other/new"
    return _render("key_created.ezt", result)


@APP.post("/keys/revoke")
@asfquart.auth.require
async def keys_revoke():
    form = await quart.request.form
    token_id = (form.get("token_id") or form.get("token") or "").strip()
    after_path = _safe_after_path(form.get("after_path"))
    try:
        ident = await current_identity(APP.cfg)
        assert ident is not None
        seam = APP.config["LLMAO_SEAM"]
        await seam.revoke_key(ident, token_id)
        await quart.flash("Key revoked.", "success")
    except (AuthzError, BackendUnavailable) as e:
        await quart.flash(str(e), "danger")
    return quart.redirect(after_path)


@APP.get("/static/<path:filename>")
async def serve_static(filename: str):
    return await quart.send_from_directory(STATICDIR, filename)


@APP.get("/favicon.ico")
async def serve_favicon():
    return await quart.send_from_directory(STATICDIR, "favicon.ico")

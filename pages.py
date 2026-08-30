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

import functools
import pathlib
import time

import asfquart
import asfquart.auth
import asfquart.session
import asfquart.utils
import ezt
import quart
from easydict import EasyDict as edict
from dunamai import Version

from llmao.auth import current_identity
from llmao.fleet import Fleet, Server
from llmao.litellm_client import BackendUnavailable, KeyInfo
from llmao.models import ux_models
from llmao.seam import AuthzError

APP = asfquart.APP

THIS_DIR = pathlib.Path(__file__).resolve().parent
TEMPLATES = THIS_DIR / "templates"
STATICDIR = THIS_DIR / "static"

REPO_URL = "https://github.com/apache/tooling-llmao"

# STeVe-style flash helpers (Bootstrap alert categories).
flash_success = functools.partial(quart.flash, category="success")
flash_danger = functools.partial(quart.flash, category="danger")
flash_warning = functools.partial(quart.flash, category="warning")


def _render(template_name: str, data) -> str:
    return asfquart.utils.render(APP.load_template(TEMPLATES / template_name), data)


def _flash_rows():
    """Drain Quart flashes for EZT. Call immediately before use_template render."""
    try:
        msgs = quart.get_flashed_messages(with_categories=True)
    except Exception:
        return []
    return [edict(category=c, message=m) for c, m in msgs]


def page(*extra_exc, title: str = "llmao", category: str = "warning"):
    """GET pages: basic_info(title), inject result=, catch, attach flashes.

    Innermost under ``@APP.use_template``. Views take ``result`` as a keyword
    (Quart path params stay named kwargs).
    """
    types = (AuthzError, BackendUnavailable) + extra_exc

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            result = await basic_info(title)
            try:
                data = await fn(*args, result=result, **kwargs)
            except types as e:
                await quart.flash(str(e), category)
                data = result
            if data is None:
                data = result
            data.flashes = _flash_rows()
            return data

        return wrapper

    return deco


async def basic_info(title: str = "llmao") -> edict:
    """Base-level EZT template data shared by HTML pages."""
    basic = edict()
    basic.title = title
    basic.flashes = []

    # Form defaults for create pages
    basic.form_project = ""
    basic.form_purpose = ""
    basic.keys_back = "/keys"
    basic.keys_create_another = "/keys/new"

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
        is_site_admin = (
            client_session.uid in site_admins
            or bool(getattr(client_session, "isRoot", False))
        )
        basic.is_site_admin = ezt.boolean(is_site_admin)
        # Other Keys nav + automation mint (provisional: PMC or site admin).
        basic.can_create_automation = ezt.boolean(is_site_admin or bool(committees))
        if is_site_admin:
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
        basic.is_site_admin = ezt.boolean(False)
        basic.can_create_automation = ezt.boolean(False)
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
            "is_automation": ezt.boolean(k.is_automation),
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
@page(title="Home")
async def home_page(result):
    return result


@APP.get("/models")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "models.ezt")
@page(FileNotFoundError, ValueError, OSError, title="Models")
async def models_page(result):
    """Gateway model inventory (public fields; supply path for site admins)."""
    result.reveal_supply = bool(result.is_site_admin)
    fleet = APP.fleet
    rows = []
    for m in ux_models(cfg=APP.cfg, reveal_supply=result.reveal_supply):
        row = edict(m)
        row.health = fleet.model_health(row.model_name)
        row.health_up = ezt.boolean(row.health == Fleet.BADGE_UP)
        row.health_starting = ezt.boolean(row.health == Fleet.BADGE_STARTING)
        row.health_down = ezt.boolean(row.health == Fleet.BADGE_DOWN)
        row.health_mixed = ezt.boolean(row.health == Fleet.BADGE_MIXED)
        rows.append(row)
    result.models = rows
    return result


def _ago(ts, now: float) -> str:
    if ts is None:
        return "never"
    sec = max(0, int(now - ts))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h"


@APP.get("/fleet")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "fleet.ezt")
@page(title="Fleet")
async def fleet_page(result):
    if not result.is_site_admin:
        raise AuthzError("site admin only")
    now = time.time()
    fleet = APP.fleet
    litellm = APP.cfg.litellm.base_url.rstrip("/")
    result.litellm_ui = f"{litellm}/ui"
    rows = []
    for srv in fleet.servers:
        fetched = fleet.config_fetch_at.get(srv.set_id)
        rows.append(edict({
            "set_id": srv.set_id,
            "name": srv.name,
            "host_port": f"{srv.host}:{srv.port}",
            "state": srv.state,
            "last_ok": _ago(srv.last_ok, now),
            "config_ago": _ago(fetched, now),
            "skew": "; ".join(srv.skew) if srv.skew else "",
            "healthy": ezt.boolean(srv.state == Server.HEALTHY),
            "starting": ezt.boolean(srv.state == Server.STARTING),
            "down": ezt.boolean(srv.state == Server.DOWN),
        }))
    result.servers = rows
    return result


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def _project_list_rows(rows) -> list:
    out = []
    for r in rows:
        if r.pct_used is None:
            pct_label = "—"
        else:
            pct_label = f"{r.pct_used:.0f}%"
        out.append(edict({
            "name": r.project,
            "href": f"/projects/{r.project}",
            "is_steward": ezt.boolean(r.is_steward),
            "spend": _money(r.spend),
            "max_budget": _money(r.max_budget),
            "remaining": _money(r.remaining),
            "pct_label": pct_label,
            "budget_duration": r.budget_duration,
            "grantor": r.grantor,
        }))
    return out


@APP.get("/projects")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "projects.ezt")
@page(title="Projects")
async def projects_list(result):
    """Projects you belong to, with project-budget summary."""
    ident = await current_identity(APP.cfg)
    result.project_rows = _project_list_rows(
        await APP.config["LLMAO_SEAM"].list_projects_for(ident)
    )
    return result


@APP.get("/projects/<project>")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "project.ezt")
@page()
async def project_stub(result, project: str):
    """Member-gated stub until P0.3 overview."""
    result.title = project
    result.project = project
    await APP.config["LLMAO_SEAM"].team_status(
        await current_identity(APP.cfg), project
    )
    return result


def _safe_after_path(raw: str | None, default: str = "/keys") -> str:
    """Only allow in-app keys paths as post-revoke/create redirects."""
    if raw in ("/keys", "/keys/other"):
        return raw
    return default


def _see_other(path: str):
    """303 See Other — next request is GET (STeVe mutation pattern)."""
    return quart.redirect(path, code=303)


async def _flash_key_created(created, *, kind_label: str, keys_back: str, keys_create_another: str) -> None:
    """Render the created-key fragment and stash it as a raw HTML flash."""
    data = edict({
        "secret": created.secret,
        "purpose": created.info.purpose or "—",
        "project": created.info.project,
        "kind_label": kind_label,
        "is_automation": ezt.boolean(created.info.is_automation),
        "created_by": created.info.created_by or "",
        "keys_back": keys_back,
        "keys_create_another": keys_create_another,
    })
    html = _render("flash_key_created.ezt", data)
    await quart.flash(html, "raw")


@APP.get("/keys")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "keys.ezt")
@page(title="My Keys")
async def keys_list(result):
    """My Keys — personal PATs only (one list_keys call)."""
    ident = await current_identity(APP.cfg)
    result.keys = _key_rows(
        await APP.config["LLMAO_SEAM"].list_my_keys(ident), after_path="/keys"
    )
    return result


@APP.get("/keys/other")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "keys_other.ezt")
@page(title="Other Keys")
async def keys_other_list(result):
    """Other Keys — automation / team-scoped (PMC / site admin)."""
    if not result.can_create_automation:
        raise AuthzError("Other Keys is limited to PMC members and site admins.")
    ident = await current_identity(APP.cfg)
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
    return result


@APP.get("/keys/new")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "key_create.ezt")
@page(title="Create personal key")
async def keys_new_form(result):
    return result


@APP.post("/do-create-key")
@asfquart.auth.require
async def do_create_key():
    form = await quart.request.form
    project = (form.get("project") or "").strip()
    purpose = (form.get("purpose") or "").strip()
    try:
        ident = await current_identity(APP.cfg)
        seam = APP.config["LLMAO_SEAM"]
        created = await seam.create_personal_key(ident, project, purpose)
    except (AuthzError, BackendUnavailable) as e:
        await flash_danger(str(e))
        return _see_other("/keys/new")
    await _flash_key_created(
        created,
        kind_label="Personal",
        keys_back="/keys",
        keys_create_another="/keys/new",
    )
    return _see_other("/keys")


@APP.get("/keys/other/new")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "key_create_other.ezt")
@page(title="Create automation key")
async def keys_other_new_form(result):
    if not result.can_create_automation:
        raise AuthzError("Only PMC members and site admins may create automation keys.")
    return result


@APP.post("/do-create-other-key")
@asfquart.auth.require
async def do_create_other_key():
    form = await quart.request.form
    project = (form.get("project") or "").strip()
    purpose = (form.get("purpose") or "").strip()
    ident = await current_identity(APP.cfg)
    if not (ident.is_site_admin or ident.committees):
        await flash_danger(
            "Only PMC members and site admins may create automation keys."
        )
        return _see_other("/keys/other/new")
    try:
        seam = APP.config["LLMAO_SEAM"]
        created = await seam.create_automation_key(ident, project, purpose)
    except (AuthzError, BackendUnavailable) as e:
        await flash_danger(str(e))
        return _see_other("/keys/other/new")
    await _flash_key_created(
        created,
        kind_label="Automation",
        keys_back="/keys/other",
        keys_create_another="/keys/other/new",
    )
    return _see_other("/keys/other")


@APP.post("/do-revoke-key")
@asfquart.auth.require
async def do_revoke_key():
    form = await quart.request.form
    token_id = (form.get("token_id") or form.get("token") or "").strip()
    after_path = _safe_after_path(form.get("after_path"))
    try:
        ident = await current_identity(APP.cfg)
        seam = APP.config["LLMAO_SEAM"]
        await seam.revoke_key(ident, token_id)
        await flash_success("Key revoked.")
    except (AuthzError, BackendUnavailable) as e:
        await flash_danger(str(e))
    return _see_other(after_path)


@APP.get("/static/<path:filename>")
async def serve_static(filename: str):
    return await quart.send_from_directory(STATICDIR, filename)


@APP.get("/favicon.ico")
async def serve_favicon():
    return await quart.send_from_directory(STATICDIR, "favicon.ico")

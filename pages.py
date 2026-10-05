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
from urllib.parse import urlsplit

import asfquart
import asfquart.auth
import asfquart.session
import asfquart.utils
import ezt
import quart
from dunamai import Version
from easydict import EasyDict as edict  # noqa: N813

from llmao.auth import current_identity
from llmao.fleet import add_refusal, remove_refusal, snapshot_for_status
from llmao.litellm_client import BackendUnavailableError, KeyInfo
from llmao.model_status import (
    ADMIN_LABELS,
    AVAILABLE,
    AWAITING,
    DEGRADED,
    EXTERNAL,
    HEALTHY,
    LOADING,
    PRIVATE,
    SKEW_PHRASE,
    STALLED,
    UNAVAILABLE,
    UNHEALTHY,
    deployment_status,
)
from llmao.models import model_available_for, model_in_service, ux_models
from llmao.seam import AuthzError

# Replica summary order: the calm states first, the ones needing attention last.
_REPLICA_ORDER = (HEALTHY, LOADING, AWAITING, "no deployment", "pending", UNHEALTHY, STALLED)

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


def _safe_back(default: str = "/") -> str:
    """Where to send someone after a handler raised.

    Referer is client-supplied, so it is only followed when it is same-origin
    and is not the path that just failed:

    - off-site is refused, or llmao becomes an open redirect: an attacker
      sends a victim to a failing llmao URL and we bounce them onward with an
      apache.org address in their history
    - the failing path itself is refused, or refreshing it redirects to
      itself forever
    - no Referer at all means a bookmark, a pasted link or a typed URL, so
      there is nowhere to go back to
    """
    ref = quart.request.headers.get("Referer") or ""
    if not ref:
        return default
    parsed = urlsplit(ref)
    if parsed.netloc and parsed.netloc != quart.request.host:
        return default
    target = parsed.path or default
    if target == quart.request.path:
        return default
    return target


def page(*extra_exc, title: str = "llmao", category: str = "warning"):
    """GET pages: basic_info(title), inject result=, catch, attach flashes.

    Innermost under ``@APP.use_template``. Views take ``result`` as a keyword
    (Quart path params stay named kwargs).
    """
    types = (AuthzError, BackendUnavailableError, *extra_exc)

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            result = await basic_info(title)
            try:
                data = await fn(*args, result=result, **kwargs)
            except types as e:
                # Redirect rather than falling through to the render.
                #
                # `data = result` handed the page template the same object
                # the aborted handler was midway through filling, so anything
                # assigned after the raise was simply absent. ezt then failed
                # on the missing name and Quart returned a 500 with the full
                # traceback -- session dict included. That is more disclosure
                # for a rejected request than a successful one gets.
                #
                # Worse when it does not crash: if the template happens to
                # read only names set before the raise, it renders a page
                # that looks right and is quietly missing data.
                #
                # Authz was how this surfaced, but BackendUnavailableError is the
                # one that will recur -- a handler that makes two LiteLLM
                # calls, where the second fails, hits it during any proxy
                # restart.
                await quart.flash(str(e), category)
                return quart.redirect(_safe_back())
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
        is_site_admin = client_session.uid in site_admins or bool(getattr(client_session, "isRoot", False))
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
        rows.append(
            edict(
                {
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
                }
            )
        )
    return rows


@APP.get("/")
@APP.use_template(TEMPLATES / "home.ezt")
@page(title="Home")
async def home_page(result):
    return result


_ROLLUP_LABEL = {
    AVAILABLE: "Available",
    DEGRADED: "Degraded",
    UNAVAILABLE: "Unavailable",
}

_PRIVACY_TEXT = {
    PRIVATE: "Prompts stay on infrastructure we control.",
    EXTERNAL: "Prompts are sent to an external provider.",
}


def _modality_chips(modality: str) -> list[edict]:
    """Labeled capability chips. models.yaml stores 'text' or 'text+vision'."""
    chips = []
    parts = {p.strip().lower() for p in (modality or "").replace("+", " ").split() if p.strip()}
    if "text" in parts:
        chips.append(edict(label="Text", title="Accepts text"))
    if "vision" in parts:
        chips.append(edict(label="Vision", title="Accepts images"))
    return chips


def _replica_summary(model_name: str, fleet, *, now: float) -> str:
    """'1 healthy / 3 loading / 1 unhealthy' over the model's deployments."""
    cfg = fleet.cfg.fleet
    views = []
    for dep in fleet.deployments:
        if dep.model_name != model_name:
            continue
        views.append(deployment_status(snapshot_for_status(dep, cfg, now), cfg, now))
    by_state: dict[str, int] = {}
    for v in views:
        by_state[v.lifecycle] = by_state.get(v.lifecycle, 0) + 1
    order = {state: i for i, state in enumerate(_REPLICA_ORDER)}
    parts = [f"{n} {state}" for state, n in sorted(by_state.items(), key=lambda kv: order.get(kv[0], len(order)))]
    return " / ".join(parts)


_LABEL_OPTIONS = [
    edict(value="", text="— no label —"),
    edict(value="approved", text="Approved"),
    edict(value="reboot_requested", text="Reboot requested"),
]


def _label_fields(fleet, host: str, *, now: float) -> dict:
    """The label display fields for a strip row. Unknown boxes carry the
    set/clear control; everything else labels nothing."""
    if not host:
        return {"host": "", "label": "", "label_value": "", "label_at": "", "label_options": []}
    label_value, label_at = fleet.admin_label(host)
    label_value = label_value or ""
    return {
        "host": host,
        "label": label_value.replace("_", " "),
        "label_value": label_value,
        "label_at": _ago(label_at, now) if label_at else "",
        "label_options": _LABEL_OPTIONS,
    }


def attention_items(fleet, *, now: float) -> list:
    """The admin strip: only actionable items, in the order they were listed
    in the design (unknown boxes, Stalled, Unhealthy, true skew)."""
    items: list[edict] = []
    for host, rec in sorted(
        fleet.unknown_config_fetches.items(),
        key=lambda item: item[1]["last_seen"],
        reverse=True,
    ):
        items.append(
            edict(
                kind="unknown",
                what=f"Fleet key presented from {host} (not in fleet.hosts)",
                detail=f"seen {_ago(rec['last_seen'], now)} · {int(rec['count'])} requests",
                **_label_fields(fleet, host, now=now),
            )
        )
    cfg = fleet.cfg.fleet
    for dep in fleet.deployments:
        view = deployment_status(snapshot_for_status(dep, cfg, now), cfg, now)
        if view.lifecycle in (STALLED, UNHEALTHY):
            items.append(
                edict(
                    kind=view.lifecycle,
                    what=f"{dep.name} is {view.lifecycle.replace('_', ' ')}",
                    detail=dep.model_name,
                    **_label_fields(fleet, "", now=now),
                )
            )
        for badge in dep.skew:
            phrase = SKEW_PHRASE.get(badge, badge)
            items.append(edict(kind="skew", what=phrase, detail=dep.name, **_label_fields(fleet, "", now=now)))
    for extra in fleet.extra_litellm:
        for badge in extra.skew:
            items.append(
                edict(
                    kind="skew",
                    what=SKEW_PHRASE.get(badge, badge),
                    detail=f"{extra.host}:{extra.port} {extra.model_name}",
                    **_label_fields(fleet, "", now=now),
                )
            )
    return items


def model_catalog_rows(fleet, catalog: list, *, now: float | None = None, admin: bool = False) -> list:
    """One Models-page row per catalog entry, with the user-facing roll-up.

    Context and concurrency come from the roll-up (minimum context across
    healthy deployments; concurrency summed over healthy self-hosted ones),
    so a catalog figure cannot overstate a degraded replica. For admins the
    row also carries the model's deployment rows and a replica summary.
    Kept out of models_page so a test can render models.ezt without a request.
    """
    stamp = time.time() if now is None else now
    rows = []
    for m in catalog:
        row = edict(m)
        status = fleet.model_rollup(row.model_name, now=now)
        rollup = status.rollup
        self_hosted = bool(m.get("self_hosted"))
        in_service = model_available_for(None, m) and model_in_service(rollup)
        if self_hosted:
            in_service = in_service and fleet.model_in_litellm(row.model_name)
        row.rollup = rollup
        row.rollup_label = _ROLLUP_LABEL.get(rollup, rollup)
        row.available = ezt.boolean(in_service)
        row.degraded = ezt.boolean(rollup == DEGRADED and in_service)
        row.unavailable = ezt.boolean(not in_service)
        row.context_window = f"{status.context_window:,}" if status.context_window else "—"
        if status.concurrency:
            row.concurrency = f"Handles up to {status.concurrency} full-context requests at once"
        else:
            row.concurrency = ""
        privacy = status.privacy
        row.private = ezt.boolean(privacy == PRIVATE)
        row.privacy = _PRIVACY_TEXT.get(privacy, "")
        row.chips = _modality_chips(m.get("modality") or "")
        if admin:
            row.deployments = [
                r for r in deployment_rows(fleet, admin=True, now=stamp) if r.model_name == row.model_name
            ]
            row.replica_summary = _replica_summary(row.model_name, fleet, now=stamp)
        rows.append(row)
    rows.sort(key=lambda r: (bool(r.unavailable), (r.display_name or "").lower()))
    for i, row in enumerate(rows):
        row.index = i
    return rows


@APP.get("/models")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "models.ezt")
@page(FileNotFoundError, ValueError, OSError, title="Models")
async def models_page(result):
    """Gateway model inventory (public fields; supply path for site admins)."""
    admin = bool(result.is_site_admin)
    result.reveal_supply = admin
    catalog = ux_models(cfg=APP.cfg, reveal_supply=admin)
    result.models = model_catalog_rows(APP.fleet, catalog, admin=admin)
    result.attention = attention_items(APP.fleet, now=time.time()) if admin else []
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
    now = time.time()
    fleet = APP.fleet
    admin = bool(result.is_site_admin)
    litellm = APP.cfg.litellm.base_url.rstrip("/")
    result.litellm_ui = f"{litellm}/ui" if admin else ""
    result.servers = fleet_rows(fleet, admin=admin, now=now)
    return result


def unknown_fetch_rows(fleet, *, now: float) -> list:
    """Admin rows for fleet-key fetches from IPs not in fleet.hosts."""
    rows = []
    unknown = sorted(
        fleet.unknown_config_fetches.items(),
        key=lambda item: item[1]["last_seen"],
        reverse=True,
    )
    for host, rec in unknown:
        label_value, label_at = fleet.admin_label(host)
        rows.append(
            edict(
                host=host,
                name="—",
                model_name="",
                self_hosted=ezt.boolean(False),
                listen="—",
                public="—",
                state="Fleet key presented; this IP is not in config.yaml",
                detail="",
                last_ok="—",
                config_ago=f"{_ago(rec['last_seen'], now)} · {int(rec['count'])}",
                skew="",
                kv_cache="—",
                context="—",
                oversized=ezt.boolean(False),
                in_litellm=ezt.boolean(False),
                litellm_health="—",
                litellm_health_ago="",
                no_deployment=ezt.boolean(False),
                serving=ezt.boolean(False),
                loading=ezt.boolean(False),
                awaiting=ezt.boolean(False),
                unhealthy=ezt.boolean(False),
                pending=ezt.boolean(False),
                unknown=ezt.boolean(True),
                row_class="table-warning",
                show_add=ezt.boolean(False),
                add_enabled=ezt.boolean(False),
                label=label_value.replace("_", " ") if label_value else "",
                label_value=label_value or "",
                label_at=_ago(label_at, now) if label_at else "",
                label_options=_LABEL_OPTIONS,
            )
        )
    return rows


def deployment_rows(fleet, *, admin: bool, now: float) -> list:
    """One table row per intended deployment, admin fields gated on ``admin``."""
    rows = []
    for dep in fleet.deployments:
        srv = dep.vllm
        fetched = fleet.config_fetch_at.get(srv.host) if srv else None
        # The admin label is a host-scoped annotation; unknown-fetch rows
        # above already carry the host and are labeled separately.
        label_value, label_at = ("", "")
        if admin and srv is not None:
            label_value, label_at = fleet.admin_label(srv.host)
            label_value = label_value or ""
            label_at = _ago(label_at, now) if label_at else ""
        label = label_value.replace("_", " ")
        if srv is not None:
            last_ok = srv.last_ok
            no_deployment = srv.state == HEALTHY and not dep.in_litellm
            serving = srv.state == HEALTHY and dep.in_litellm
            loading = srv.state == LOADING
            unhealthy = srv.state in (UNHEALTHY, STALLED)
            awaiting = srv.state == AWAITING
            pending = False
            state = srv.state
            # Awaiting contact: the box has not fetched its config yet, so
            # there is nothing to load. Loading has a detail: vLLM is either
            # up with a 503 while the model loads, or not listening at all.
            detail = ""
            if srv.state == AWAITING:
                detail = "has not requested its config"
            elif loading and srv.reached is not None:
                detail = "vLLM up, model loading" if srv.reached else "vLLM not listening yet"
            listen = f"{srv.host}:{srv.listen_port}" if admin else ""
            public = (
                f"{srv.host}:{srv.public_port}" if admin and srv.public_port is not None else ("—" if admin else "")
            )
            host = srv.host if admin else ""
            config_ago = _ago(fetched, now) if admin else ""
            # Measured KV cache against the served context window. A
            # max_model_len above the cache makes vLLM hang on a request that
            # needs the space rather than refuse at startup, so it reads as a
            # slow model rather than a misconfiguration.
            kv_cache = f"{srv.kv_cache_tokens:,}" if srv.kv_cache_tokens is not None else "—"
            served_len = srv.observed_max_model_len or srv.max_model_len
            context = f"{served_len:,}" if served_len else "—"
            oversized = srv.oversized
        else:
            last_ok = dep.litellm_health_at
            no_deployment = not dep.in_litellm
            serving = dep.in_litellm and dep.litellm_healthy is True
            loading = False
            unhealthy = dep.in_litellm and dep.litellm_healthy is False
            pending = dep.in_litellm and dep.litellm_healthy is None
            if serving:
                state = "healthy"
            elif unhealthy:
                state = "unhealthy"
            elif pending:
                state = "pending"
            else:
                state = "no deployment"
            detail = ""
            listen = "—" if admin else ""
            public = dep.api_base if admin else ""
            host = ""
            config_ago = "—" if admin else ""
            # Commercial endpoints have no KV cache to measure.
            kv_cache = "—"
            context = "—"
            oversized = False
        rows.append(
            edict(
                host=host,
                name=dep.name,
                model_name=dep.model_name,
                self_hosted=ezt.boolean(dep.self_hosted),
                listen=listen,
                public=public,
                state=state,
                detail=detail,
                last_ok=_ago(last_ok, now),
                config_ago=config_ago,
                skew="; ".join(SKEW_PHRASE.get(n, n) for n in dep.skew) if admin and dep.skew else "",
                kv_cache=kv_cache,
                context=context,
                oversized=ezt.boolean(oversized),
                in_litellm=ezt.boolean(dep.in_litellm),
                litellm_health=(
                    "healthy" if dep.litellm_healthy is True else ("unhealthy" if dep.litellm_healthy is False else "—")
                ),
                litellm_health_ago=(
                    _ago(dep.litellm_health_at, now) if admin and dep.litellm_health_at else ("—" if admin else "")
                ),
                no_deployment=ezt.boolean(no_deployment),
                serving=ezt.boolean(serving),
                loading=ezt.boolean(loading),
                awaiting=ezt.boolean(awaiting),
                unhealthy=ezt.boolean(unhealthy),
                pending=ezt.boolean(pending),
                unknown=ezt.boolean(False),
                row_class="",
                show_add=ezt.boolean(admin and dep.self_hosted and not dep.in_litellm),
                add_enabled=ezt.boolean(admin and add_refusal(dep) is None),
                show_remove=ezt.boolean(admin and dep.self_hosted and dep.in_litellm),
                remove_enabled=ezt.boolean(admin and remove_refusal(dep) is None),
                label=label,
                label_value=label_value,
                label_at=label_at,
                label_options=_LABEL_OPTIONS,
            )
        )
    return rows


def fleet_rows(fleet, *, admin: bool, now: float) -> list:
    """The /fleet page's rows: unknown fetches (admin) above the deployments."""
    rows = unknown_fetch_rows(fleet, now=now) if admin else []
    rows.extend(deployment_rows(fleet, admin=admin, now=now))
    return rows


@APP.post("/do-add-deployment")
@asfquart.auth.require
async def do_add_deployment():
    """Site admin posts one self-hosted row. add_refusal is the gate."""
    ident = await current_identity(APP.cfg)
    if not ident.is_site_admin:
        await flash_danger("Only site admins can add a fleet deployment.")
        return _see_other("/fleet")
    form = await quart.request.form
    name = (form.get("name") or "").strip()
    host = (form.get("host") or "").strip()
    matches = [d for d in APP.fleet.deployments if d.name == name and d.vllm is not None and d.vllm.host == host]
    if len(matches) != 1:
        await flash_danger(f"No deployment {name} on {host}.")
        return _see_other("/fleet")
    reason = add_refusal(matches[0])
    if reason:
        await flash_danger(reason)
        return _see_other("/fleet")
    try:
        await APP.backend.add_deployment(matches[0])
    except BackendUnavailableError as e:
        await flash_danger(str(e))
        return _see_other("/fleet")
    await flash_success(f"Added {matches[0].name} at {matches[0].api_base}.")
    return _see_other("/fleet")


@APP.post("/do-remove-deployment")
@asfquart.auth.require
async def do_remove_deployment():
    """Site admin drops one self-hosted LiteLLM route. remove_refusal is the gate."""
    ident = await current_identity(APP.cfg)
    if not ident.is_site_admin:
        await flash_danger("Only site admins can remove a fleet deployment.")
        return _see_other("/fleet")
    form = await quart.request.form
    name = (form.get("name") or "").strip()
    host = (form.get("host") or "").strip()
    matches = [d for d in APP.fleet.deployments if d.name == name and d.vllm is not None and d.vllm.host == host]
    if len(matches) != 1:
        await flash_danger(f"No deployment {name} on {host}.")
        return _see_other("/fleet")
    reason = remove_refusal(matches[0])
    if reason:
        await flash_danger(reason)
        return _see_other("/fleet")
    try:
        await APP.backend.delete_deployment(matches[0])
    except BackendUnavailableError as e:
        await flash_danger(str(e))
        return _see_other("/fleet")
    await flash_success(f"Removed the route for {matches[0].name} at {matches[0].api_base}.")
    return _see_other("/fleet")


def apply_admin_label(fleet, host: str, label: str) -> tuple[bool, str]:
    """Parse and apply a host's admin label; the testable core of /do-set-admin-label.

    ``label`` is a member of ``ADMIN_LABELS``, or ``""`` to clear; any other
    non-empty value is rejected rather than silently treated as a clear (a
    garbled form must not wipe a vouch). Returns ``(ok, message)``.
    """
    host = (host or "").strip()
    label = (label or "").strip()
    if not host:
        return False, "No host to label."
    if label and label not in ADMIN_LABELS:
        return False, f"Unknown admin label: {label!r}."
    fleet.set_admin_label(host, label or None)
    if not label:
        return True, f"Cleared the label for {host}."
    return True, f"Marked {host} as {label.replace('_', ' ')}."


@APP.post("/do-set-admin-label")
@asfquart.auth.require
async def do_set_admin_label():
    """Site admin sets, changes, or clears a host's admin label.

    The label is an in-memory, host-scoped annotation (see
    ``Fleet.set_admin_label`` and fleet-state.md section 5.2); it is never
    read by the lifecycle and is lost on a control-plane restart.
    """
    ident = await current_identity(APP.cfg)
    if not ident.is_site_admin:
        await flash_danger("Only site admins can set an admin label.")
        return _see_other("/models")
    form = await quart.request.form
    ok, message = apply_admin_label(APP.fleet, form.get("host"), form.get("label"))
    if ok:
        await flash_success(message)
    else:
        await flash_danger(message)
    return _see_other("/models")


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def _project_list_rows(rows) -> list:
    out = []
    for r in rows:
        if r.pct_used is None:
            pct_label = "—"
        else:
            pct_label = f"{r.pct_used:.0f}%"
        out.append(
            edict(
                {
                    "name": r.project,
                    "href": f"/projects/{r.project}",
                    "is_steward": ezt.boolean(r.is_steward),
                    "spend": _money(r.spend),
                    "max_budget": _money(r.max_budget),
                    "remaining": _money(r.remaining),
                    "pct_label": pct_label,
                    "budget_duration": r.budget_duration,
                    "grantor": r.grantor,
                }
            )
        )
    return out


@APP.get("/projects")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "projects.ezt")
@page(title="Projects")
async def projects_list(result):
    """Projects you belong to, with project-budget summary."""
    ident = await current_identity(APP.cfg)
    result.project_rows = _project_list_rows(await APP.seam.list_projects_for(ident))
    return result


@APP.get("/projects/<project>")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "project.ezt")
@page()
async def project_stub(result, project: str):
    """Member-gated stub until P0.3 overview."""
    result.title = project
    result.project = project
    await APP.seam.team_status(await current_identity(APP.cfg), project)
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
    data = edict(
        {
            "secret": created.secret,
            "purpose": created.info.purpose or "—",
            "project": created.info.project,
            "kind_label": kind_label,
            "is_automation": ezt.boolean(created.info.is_automation),
            "created_by": created.info.created_by or "",
            "keys_back": keys_back,
            "keys_create_another": keys_create_another,
        }
    )
    html = _render("flash_key_created.ezt", data)
    await quart.flash(html, "raw")


@APP.get("/keys")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "keys.ezt")
@page(title="My Keys")
async def keys_list(result):
    """My Keys — personal PATs only (one list_keys call)."""
    ident = await current_identity(APP.cfg)
    result.keys = _key_rows(await APP.seam.list_my_keys(ident), after_path="/keys")
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
    seam = APP.seam
    admin_projects = ident.all_projects() if ident.is_site_admin else list(ident.committees)
    by_id: dict[str, KeyInfo] = {}
    for p in admin_projects:
        try:
            for k in await seam.list_automation_keys(ident, p):
                by_id.setdefault(k.token_id, k)
        except (AuthzError, BackendUnavailableError):
            continue
    result.keys = _key_rows(list(by_id.values()), after_path="/keys/other")
    return result


@APP.get("/keys/new")
@asfquart.auth.require
@APP.use_template(TEMPLATES / "key_create.ezt")
@page(title="Create personal key")
async def keys_new_form(result):
    model = (quart.request.args.get("model") or "").strip()
    result.for_model = model
    result.has_for_model = ezt.boolean(bool(model))
    return result


@APP.post("/do-create-key")
@asfquart.auth.require
async def do_create_key():
    form = await quart.request.form
    project = (form.get("project") or "").strip()
    purpose = (form.get("purpose") or "").strip()
    try:
        ident = await current_identity(APP.cfg)
        seam = APP.seam
        created = await seam.create_personal_key(ident, project, purpose)
    except (AuthzError, BackendUnavailableError) as e:
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
        await flash_danger("Only PMC members and site admins may create automation keys.")
        return _see_other("/keys/other/new")
    try:
        seam = APP.seam
        created = await seam.create_automation_key(ident, project, purpose)
    except (AuthzError, BackendUnavailableError) as e:
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
        seam = APP.seam
        await seam.revoke_key(ident, token_id)
        await flash_success("Key revoked.")
    except (AuthzError, BackendUnavailableError) as e:
        await flash_danger(str(e))
    return _see_other(after_path)


@APP.get("/static/<path:filename>")
async def serve_static(filename: str):
    return await quart.send_from_directory(STATICDIR, filename)


@APP.get("/favicon.ico")
async def serve_favicon():
    return await quart.send_from_directory(STATICDIR, "favicon.ico")

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

import datetime as dt
import functools
import io
import logging
import pathlib
import re
import time
from urllib.parse import quote, urlsplit

import asfquart
import asfquart.auth
import asfquart.generics
import asfquart.session
import asfquart.utils
import cmarkgfm
import ezt
import quart
import werkzeug.sansio.response
from asfpy.stopwatch import Stopwatch
from cmarkgfm.cmark import Options
from dunamai import Version
from easydict import EasyDict as edict  # noqa: N813

from llmao.access_requests import APPROVAL_TTL_DAYS
from llmao.auth import current_identity
from llmao.capacity import days_to_read, summarise_capacity, summarise_spend
from llmao.fleet import (
    ORPHAN_PASSES,
    add_refusal,
    extra_refusal,
    normalize_peer_ip,
    remove_refusal,
    snapshot_for_status,
)
from llmao.model_status import (
    AVAILABLE,
    AWAITING,
    CONFIGURED,
    DEGRADED,
    EXTERNAL,
    FOREIGN,
    HEALTHY,
    LOADING,
    MIXED,
    PRIVATE,
    REJECTED,
    SKEW_PHRASE,
    STALLED,
    UNAVAILABLE,
    UNHEALTHY,
    UNKNOWN,
    deployment_status,
)
from llmao.models import load_models, model_available_for, model_in_service, ux_models, vllm_recipe
from llmao.proxy.client import (
    PRETEND_PROJECTS,
    TIER_FREE,
    TIERS,
    BackendUnavailableError,
    KeyInfo,
    pinned_litellm_version,
)
from llmao.rental import match_rental
from llmao.seam import AuthzError

# Replica summary order: the calm states first, the ones needing attention last.
_REPLICA_ORDER = (HEALTHY, LOADING, AWAITING, "no deployment", "pending", UNHEALTHY, STALLED, REJECTED, FOREIGN)

APP = asfquart.APP

_LOGGER = logging.getLogger(__name__)

THIS_DIR = pathlib.Path(__file__).resolve().parent
TEMPLATES = THIS_DIR / "templates"
STATICDIR = THIS_DIR / "static"

REPO_URL = "https://github.com/apache/tooling-llmao"
# Import is process start: create_app loads this module once, and a restart is a new process.
# The server clock is UTC. The label says so, rather than shifting to an admin's zone.
RESTARTED_AT = time.strftime("%m/%d %H:%M", time.gmtime()) + " (UTC)"
# Read once, for the same reason: the commit this process loaded is the one it is running,
# whatever is checked out since. Asking git per request also blocked the event loop.
COMMIT = Version.from_git().commit

# STeVe-style flash helpers (Bootstrap alert categories).
flash_success = functools.partial(quart.flash, category="success")
flash_danger = functools.partial(quart.flash, category="danger")
flash_warning = functools.partial(quart.flash, category="warning")


def _render(template_name: str, data) -> str:
    return asfquart.utils.render(APP.load_template(TEMPLATES / template_name), data)


def use_template(path):
    """asfquart's use_template, except a Response is returned as it is.

    @page answers a caught AuthzError or BackendUnavailableError with a
    redirect. asfquart's wrapper handed that Response to ezt as the data
    dictionary, which failed on the first name looked up ([title] in the
    header), so every such bounce was a 500 instead. Stands in until
    asfquart passes a Response through itself.
    """
    template = APP.load_template(path)

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            data = await fn(*args, **kwargs)
            if isinstance(data, werkzeug.sansio.response.Response):
                return data
            return asfquart.utils.render(template, data)

        return wrapper

    return deco


@APP.errorhandler(500)
async def _server_error(_error):
    """An exception nothing caught: an application bug, by design.

    Quart has already logged it with the traceback by the time this runs
    (and in tests it propagates instead, so nothing is hidden there). The
    person gets a flash and the home page, not a bare "Internal Server
    Error". The home page itself failing cannot bounce to itself. An API
    client under /v1/ gets a JSON error, not a redirect to a web page.
    """
    if quart.request.path.startswith("/v1/"):
        message = "internal error in gateway; check the server log"
        return quart.jsonify(edict(error=edict(message=message, type="llmao_error", code=500))), 500
    if quart.request.path == "/":
        return "Something went wrong. It has been logged.", 500
    await flash_danger("Something went wrong on that page. It has been logged.")
    return quart.redirect("/")


def _flash_rows():
    """Drain Quart flashes for EZT. Call immediately before use_template render."""
    try:
        msgs = quart.get_flashed_messages(with_categories=True)
    except Exception:
        return []
    return [edict(category=c, message=m) for c, m in msgs]


async def _work_pointer(path: str):
    """One line for every page: there is work to do, and where to go to do it.

    The work itself is shown where there is room for it and its buttons --
    the Admin page for a site admin, My Keys for anyone else. Somebody
    reading Help is told it is waiting and is not made to look at it. On
    the page it points to there is nothing to point at, so this is None.
    """
    try:
        ident = await current_identity(APP.cfg)
        count = await APP.seam.work_count(ident)
    except (AuthzError, BackendUnavailableError):
        return None
    href, label = ("/admin", "Admin") if ident.is_site_admin else ("/keys", "My Keys")
    if not count or path == href:
        return None
    return edict(what="1 thing needs doing." if count == 1 else f"{count} things need doing.", href=href, label=label)


async def _notice_rows() -> list:
    """What the signed-in person should see, as rows for flashes.ezt.

    The same partial shows both, so there is one place on every page where
    a person is told things, but they are kept apart there. A flash answers
    what was just done and is gone on the next page; one of these stays
    until it is acknowledged. Mixed together, a person would be left
    wondering why one went away and the other did not.

    Every page calls this, so it always returns a list. The list is empty
    when the visitor is signed out or the memory store cannot be reached.
    """
    try:
        entries = await APP.seam.notices(await current_identity(APP.cfg))
    except (AuthzError, BackendUnavailableError):
        return []
    rows = []
    for n in entries:
        count = int(n.get("count") or 1)
        rows.append(
            edict(
                what=n.get("what") or "",
                key=n.key,
                times=f"{count} times" if count > 1 else "",
                when=_stamp(n.get("last_seen")),
            )
        )
    return rows


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
    - the login endpoint is refused: asfquart's OAuth callback sends the
      browser on to the page it wanted with a Refresh header, so when that
      page then refuses the person, the Referer is /auth?code=...&state=...
      Going "back" there, without those arguments, dumps the session as
      JSON
    """
    ref = quart.request.headers.get("Referer") or ""
    if not ref:
        return default
    parsed = urlsplit(ref)
    if parsed.netloc and parsed.netloc != quart.request.host:
        return default
    target = parsed.path or default
    if target in (quart.request.path, asfquart.generics.DEFAULT_OAUTH_URI):
        return default
    return target


def page(*extra_exc, title: str = "llmao", category: str = "warning"):
    """GET pages: basic_info(title), inject result=, catch, attach flashes.

    Innermost under ``@use_template``. Views take ``result`` as a keyword
    (Quart path params stay named kwargs).
    """
    types = (AuthzError, BackendUnavailableError, *extra_exc)

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            with Stopwatch(name=f"Page {fn.__name__}: basic_info"):
                result = await basic_info(title)
            try:
                # The handler alone: template rendering happens after this
                # returns, so time missing from here and from the LiteLLM
                # lines is the render or the session.
                with Stopwatch(name=f"Page {fn.__name__}: handler"):
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
            # Three things a page can have to tell a person, kept apart
            # because each goes away differently: a flash on the next page,
            # work when it is done, a notice when it is acknowledged.
            data.work = await _work_pointer(quart.request.path)
            data.notices = await _notice_rows()
            return data

        return wrapper

    return deco


_NAV_SECTIONS = ("keys", "models", "projects", "admin", "help")


def _nav_section(path: str) -> str:
    """Which top-nav link a path belongs under, or "" for none (home)."""
    first = path.strip("/").split("/", 1)[0]
    return first if first in _NAV_SECTIONS else ""


async def basic_info(title: str = "llmao") -> edict:
    """Base-level EZT template data shared by HTML pages."""
    basic = edict()
    basic.title = title
    basic.flashes = []
    basic.work = None
    basic.notices = []
    path = quart.request.path if quart.has_request_context() else "/"
    basic.nav = _nav_section(path)
    # Signing in returns to the page it was started from.
    basic.login_back = quote(path, safe="/")

    # Form defaults for create pages
    basic.form_project = ""
    basic.form_purpose = ""
    basic.keys_back = "/keys"
    basic.keys_create_another = "/keys/new"

    client_session = await asfquart.session.read()
    if client_session is not None and getattr(client_session, "uid", None):
        basic.uid = client_session.uid
        basic.name = getattr(client_session, "fullname", None) or client_session.uid
        projects = sorted(
            dict.fromkeys(
                list(getattr(client_session, "committees", None) or [])
                + list(getattr(client_session, "projects", None) or [])
            )
        )
        basic.projects = [edict(name=p) for p in projects]
        basic.projects_label = ", ".join(projects) if projects else None
        committees = sorted(getattr(client_session, "committees", None) or [])
        basic.committees = committees
        site_admins = sorted(APP.cfg.site_admins or [])
        is_site_admin = client_session.uid in site_admins or bool(getattr(client_session, "isRoot", False))
        basic.is_site_admin = ezt.boolean(is_site_admin)
        # Other Keys nav + automation mint (provisional: PMC or site admin).
        basic.can_create_automation = ezt.boolean(is_site_admin or bool(committees))
        if is_site_admin:
            admin_names = projects
        else:
            admin_names = committees
        basic.admin_projects = [edict(name=p) for p in admin_names]
    else:
        basic.uid = None
        basic.name = None
        basic.projects = []
        basic.projects_label = None
        basic.committees = []
        basic.is_site_admin = ezt.boolean(False)
        basic.can_create_automation = ezt.boolean(False)
        basic.admin_projects = []

    basic.commit = COMMIT
    basic.repo = REPO_URL
    basic.restarted_at = RESTARTED_AT

    return basic


def _key_rows(keys: list[KeyInfo], *, after_path: str = "/keys", used_by_token: dict[str, int] | None = None) -> list:
    """Template rows for a key table.

    `used_by_token` carries this week's draw per key. Absent, the column shows
    a dash rather than a zero -- "0" is a claim that the key is idle, and we
    do not know that.
    """
    used_by_token = used_by_token or {}
    rows = []
    for k in keys:
        budget = k.max_budget
        budget_s = f"${budget:.4f}" if budget is not None else "—"
        rows.append(
            edict(
                token_id=k.token_id,
                purpose=k.purpose or "—",
                # A pretend project is not one to the reader: the column
                # is left empty and the template says what it is instead.
                project="" if k.project in PRETEND_PROJECTS else k.project,
                no_project=PRETEND_PROJECTS.get(k.project, ""),
                kind_label="Automation" if k.is_automation else "Personal",
                is_automation=ezt.boolean(k.is_automation),
                created_by=k.created_by or "—",
                spend=f"${k.spend:.6f}",
                max_budget=budget_s,
                used_h=(_tokens_h(used_by_token[k.token_id]) if k.token_id in used_by_token else "—"),
                last_used=k.last_used or "—",
                created_at=k.created_at or "—",
                blocked=k.blocked,
                after_path=after_path,
            )
        )
    return rows


@APP.get("/")
@use_template(TEMPLATES / "home.ezt")
@page(title="Home")
async def home_page(result):
    return result


_HELP_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_HELP_HEADING = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
HELP_DIR = THIS_DIR / "docs" / "help"


def _help_title(text: str, slug: str) -> str:
    match = _HELP_HEADING.search(text)
    if match:
        return match.group(1)
    return slug


def help_article(slug: str, directory: pathlib.Path | None = None, uid=None):
    """One help article, or None when the slug is not a file in the directory.

    Raw HTML in the markdown is left intact. The files are committed here.
    A file that contains ``[if-any`` is EZT first. ``uid`` comes from
    ``basic_info`` and is None when nobody is signed in. A literal ``[``
    in that file is written ``[[]]``.
    """
    if not _HELP_SLUG.fullmatch(slug):
        return None
    root = directory or HELP_DIR
    path = root / f"{slug}.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    title = _help_title(text, slug)
    if "[if-any " in text:
        template = ezt.Template(compress_whitespace=0)
        template.parse(text, base_format=ezt.FORMAT_RAW)
        buf = io.StringIO()
        template.generate(buf, edict(uid=uid))
        text = buf.getvalue()
    return edict(
        slug=slug,
        title=title,
        html=cmarkgfm.github_flavored_markdown_to_html(text, options=Options.CMARK_OPT_UNSAFE),
    )


def help_articles(directory: pathlib.Path | None = None) -> list:
    """Every article except the index, by title."""
    root = directory or HELP_DIR
    found = []
    for path in root.glob("*.md"):
        if path.name == "index.md":
            continue
        text = path.read_text(encoding="utf-8")
        found.append(edict(slug=path.stem, title=_help_title(text, path.stem)))
    found.sort(key=lambda item: item.title.lower())
    return found


@APP.get("/help")
@APP.get("/help/<slug>")
@use_template(TEMPLATES / "help.ezt")
@page(title="Help")
async def help_page(result, slug: str = "index"):
    doc = help_article(slug, uid=result.uid)
    if doc is None:
        quart.abort(404)
    result.title = doc.title
    result.body = doc.html
    result.guides = [item for item in help_articles() if item.slug != "faq"]
    return result


_ROLLUP_LABEL = {
    AVAILABLE: "Available",
    DEGRADED: "Degraded",
    UNAVAILABLE: "Unavailable",
}

_PRIVACY_TEXT = {
    PRIVATE: (
        "Private",
        "We run every deployment on infrastructure the ASF controls. Prompts stay inside that infrastructure.",
    ),
    EXTERNAL: (
        "External",
        "Every request is sent to a third-party provider.",
    ),
    MIXED: (
        "Mixed",
        "Requests may be handled on our infrastructure or sent to a third party, and you can't choose which.",
    ),
}


# Icon beside the privacy statement. The sentence carries the meaning.
_PRIVACY_ICON = {
    PRIVATE: "bi-shield-lock",
    EXTERNAL: "bi-box-arrow-up-right",
    MIXED: "bi-shuffle",
}

# Badge per displayed state. "healthy" is split on whether LiteLLM routes to it.
_BADGE_CLASS = {
    HEALTHY: "text-bg-success",
    LOADING: "text-bg-secondary",
    AWAITING: "text-bg-info",
    UNHEALTHY: "text-bg-danger",
    STALLED: "text-bg-danger",
    REJECTED: "text-bg-danger",
    FOREIGN: "text-bg-danger",
    UNKNOWN: "text-bg-dark",
    CONFIGURED: "text-bg-light border",
    "pending": "text-bg-light border",
    "no deployment": "text-bg-warning",
}
_BADGE_DEFAULT = "text-bg-light border"


def _badge_class(state: str, *, routed: bool = True) -> str:
    """Bootstrap badge classes for one deployment state. Never empty."""
    if state == HEALTHY and not routed:
        return "text-bg-warning"
    return _BADGE_CLASS.get(state, _BADGE_DEFAULT)


def _dep_ident(dep) -> str:
    """The key's display form: host:port for a box (the listen port), the base for a provider."""
    if dep.vllm is not None or dep.api_base:
        return dep.key.display
    return dep.name


def _dep_where(dep) -> edict:
    """Where a deployment lives, for finding it: the provider and its name for the machine.

    Our machines: the provider, the Rental's label (Vast's GPUs, RunPod's
    pod name) and its id, "Vast · RTX 3060 x2 (53865410)" or "RunPod ·
    llmao-qwen3-8b (6jqrsngxdor2mc)", with the provider's status after it
    when that is not running ("..., exited"): listed, not running. A host
    no provider lists says so, which is how a machine whose rental has
    gone shows up. A model provider's deployment is its account name; its
    endpoint is the row's identity.
    """
    srv = dep.vllm
    if srv is None:
        return edict(label=dep.provider or "", url="", unknown=ezt.boolean(False))
    rental = srv.rental
    if rental is None:
        return edict(label="location unknown: no provider lists this host", url="", unknown=ezt.boolean(True))
    label = f"{rental.provider} · {rental.label} ({rental.id})"
    if rental.status and not rental.running:
        label += f", {rental.status}"
    return edict(label=label, url=rental.url or "", unknown=ezt.boolean(False))


def _dep_anchor(dep) -> str:
    """DOM id of the deployment's row, e.g. dep-10-0-0-1-8001."""
    return "dep-" + re.sub(r"[^A-Za-z0-9]+", "-", _dep_ident(dep)).strip("-")


def _needs_attention(lifecycle: str, skew, *, unlisted: bool = False) -> bool:
    """The attention strip and the row accent ask the same question."""
    return lifecycle in (STALLED, UNHEALTHY, REJECTED, FOREIGN) or bool(skew) or unlisted


def _match_deployments(fleet, name: str, host: str, port: str = "") -> list:
    """Self-hosted deployments named ``name`` on ``host``; ``port`` narrows to one listen port."""
    matches = [d for d in fleet.deployments if d.name == name and d.vllm is not None and d.vllm.host == host]
    if port:
        matches = [d for d in matches if str(d.vllm.listen_port) == port]
    return matches


def _match_extra(fleet, name: str, host: str, port: str = ""):
    """The orphan (in LiteLLM, not in config) serving ``name`` at ``host``:``port``, or None."""
    return next((x for x in fleet.extra_litellm if (x.model_name, x.host, str(x.port)) == (name, host, port)), None)


def _modality_chips(modality: str) -> list[edict]:
    """Labeled capability chips. models.yaml stores 'text' or 'text+vision'."""
    chips = []
    parts = {p.strip().lower() for p in (modality or "").replace("+", " ").split() if p.strip()}
    if "text" in parts:
        chips.append(edict(label="Text", title="Accepts text"))
    if "vision" in parts:
        chips.append(edict(label="Vision", title="Accepts images"))
    return chips


def _replica_summary(model_name: str, fleet) -> str:
    """'1 healthy / 3 loading / 1 unhealthy' over the model's deployments."""
    cfg = fleet.cfg.fleet
    views = []
    for dep in fleet.deployments:
        if dep.model_name != model_name:
            continue
        views.append(deployment_status(snapshot_for_status(dep, cfg), cfg))
    by_state: dict[str, int] = {}
    for v in views:
        by_state[v.lifecycle] = by_state.get(v.lifecycle, 0) + 1
    order = {state: i for i, state in enumerate(_REPLICA_ORDER)}
    parts = [f"{n} {state}" for state, n in sorted(by_state.items(), key=lambda kv: order.get(kv[0], len(order)))]
    return " / ".join(parts)


def _deployment_reason(dep) -> str:
    """Plain reason for one deployment. The probe result wins over 'never asked'."""
    srv = dep.vllm
    if srv is None:
        return ""
    if srv.unlisted:
        # Said first: whatever else is true, this is why. While the box still
        # answers, the listing is what is in doubt, not the rental.
        if srv.state == HEALTHY:
            return "No Vast or RunPod instance has this address, but it is serving; the listing may be behind."
        if srv.state == FOREIGN:
            return (
                "No Vast or RunPod instance has this address, and the vLLM there rejects our key; "
                "it may be someone else's now."
            )
        return "No Vast or RunPod instance has this address; the rental may have ended."
    if srv.state == REJECTED:
        return "vLLM is up but rejects our key, so it was dropped from LiteLLM. Check the api key on the box."
    if srv.state == AWAITING:
        if srv.probe_display:
            return srv.probe_display
        return "has not requested its config"
    if srv.state == LOADING and srv.reached is not None:
        if srv.reached:
            return "vLLM up, model loading"
        return "vLLM not listening yet"
    if srv.state == HEALTHY and not dep.in_litellm:
        # The yellow badge: serving, but LiteLLM sends it nothing.
        if dep.litellm_error:
            failed = f"Adding it failed {dep.litellm_error_display} ago: {dep.litellm_error}"
            return f"Not in LiteLLM, so no traffic. {failed}"
        if not dep.api_base:
            return "Not in LiteLLM, so no traffic. Added once its public address is known."
        return "Not in LiteLLM yet, so no traffic. llmao adds it on its next pass; Add does it now."
    return ""


def _action(**kwargs):
    kind = kwargs.get("kind", "")
    kwargs["is_approve"] = ezt.boolean(kind == "approve")
    kwargs["is_add"] = ezt.boolean(kind == "add")
    kwargs["is_remove"] = ezt.boolean(kind == "remove")
    kwargs.setdefault("port", "")
    kwargs.setdefault("confirm", "")
    kwargs.setdefault("title", "")
    kwargs.setdefault("aria", f"{kwargs.get('label', '')} {kwargs.get('host', '')}".strip())
    return edict(**kwargs)


def admin_actions(fleet, dep=None, *, host: str = "", approve: bool = False) -> list:
    """Buttons shared by the attention strip and the deployment rows.

    Add and Stop tracking use the existing refusal gates. A disabled action
    carries the reason as visible text. Approve is only for an unknown host.
    Add and Stop tracking act on one host:port.
    """
    actions = []
    if approve and host:
        actions.append(
            _action(
                kind="approve",
                label="Approve",
                host=host,
                name="",
                post="",
                enabled=ezt.boolean(True),
                reason="",
            )
        )
    if dep is None:
        return actions
    target = dep.vllm.host if dep.vllm is not None else host
    ident = _dep_ident(dep)
    port = str(dep.vllm.listen_port) if dep.vllm is not None else ""
    if dep.self_hosted and not dep.in_litellm:
        reason = add_refusal(dep) or ""
        actions.append(
            _action(
                kind="add",
                label="Add",
                host=target,
                name=dep.name,
                port=port,
                aria=f"Add {ident}",
                title="Register this server in LiteLLM now, so it gets traffic",
                post="/do-add-deployment",
                enabled=ezt.boolean(reason == ""),
                reason=reason,
            )
        )
    if dep.self_hosted and dep.in_litellm:
        reason = remove_refusal(dep) or ""
        actions.append(
            _action(
                kind="remove",
                label="Stop tracking",
                host=target,
                name=dep.name,
                port=port,
                aria=f"Stop tracking {ident}",
                confirm=f"Stop tracking {ident}? This drops its LiteLLM route. The instance keeps running.",
                post="/do-remove-deployment",
                enabled=ezt.boolean(reason == ""),
                reason=reason,
            )
        )
    if dep.vllm is not None and dep.vllm.unlisted:
        # The box is gone: there is nothing to reboot, add or stop tracking.
        # sync_selfhost drops its LiteLLM deployment by itself; the one thing
        # left is taking it out of fleet.hosts, which is config.
        return []
    return actions


def attention_items(fleet) -> list:
    """The admin strip: only actionable items, in the order they were listed
    in the design (unknown boxes, Stalled, Unhealthy, true skew)."""
    items: list[edict] = []
    for host, _seen in sorted(
        fleet.provisioning.unknown.items(),
        key=lambda item: item[1].last_seen,
        reverse=True,
    ):
        items.append(
            edict(
                kind="unknown",
                what=f"Fleet key presented from {host} (not in fleet.hosts)",
                ident="",
                anchor="",
                rest="",
                detail=fleet.unknown_fetch_detail(host),
                flag="",
                actions=admin_actions(fleet, host=host, approve=True),
            )
        )
    cfg = fleet.cfg.fleet
    for dep in fleet.deployments:
        view = deployment_status(snapshot_for_status(dep, cfg), cfg)
        phrases = [SKEW_PHRASE.get(n, n) for n in dep.skew]
        unlisted = dep.vllm is not None and dep.vllm.unlisted
        if not _needs_attention(view.lifecycle, dep.skew, unlisted=unlisted):
            continue
        host = dep.vllm.host if dep.vllm is not None else ""
        ident = _dep_ident(dep)
        rest = f"{dep.model_name} — {view.lifecycle.replace('_', ' ')}"
        reason = _deployment_reason(dep)
        if unlisted and view.lifecycle in (STALLED, UNHEALTHY, FOREIGN):
            # What to do about it: nothing in llmao clears a host that is gone.
            # Only once the box is also down, or answering as someone else's.
            # A listing gap with the box still serving, loading, not yet
            # tried, or refusing our key for a few passes is not that.
            reason += " If so, remove it from fleet.hosts."
        if phrases:
            reason = "; ".join(p for p in (reason, "; ".join(phrases)) if p)
        if unlisted:
            kind = "unlisted"
        elif view.lifecycle in (STALLED, UNHEALTHY, REJECTED):
            kind = view.lifecycle
        else:
            kind = "skew"
        items.append(
            edict(
                kind=kind,
                what=f"{ident} · {rest}",
                ident=ident,
                anchor=_dep_anchor(dep),
                rest=rest,
                detail=reason,
                flag=(host and fleet.approval_display(host)) or "",
                actions=admin_actions(fleet, dep),
            )
        )
    for extra in fleet.extra_litellm:
        if extra.passes < ORPHAN_PASSES:
            # A debounce: absence has innocent causes, and one read is not an item.
            continue
        phrases = [SKEW_PHRASE.get(n, n) for n in extra.skew]
        items.append(
            edict(
                kind="skew",
                what=f"{extra.host}:{extra.port} · {extra.model_name} — {'; '.join(phrases)}",
                ident="",
                anchor="",
                rest="",
                detail=_orphan_detail(extra),
                flag="",
                actions=_orphan_actions(fleet, extra),
            )
        )
    return items


def _orphan_detail(extra) -> str:
    """Who meant an orphan: the stamp is llmao's own add, no stamp is a hand add in LiteLLM."""
    if extra.llmao_key:
        return "llmao added it; its approval or fleet.hosts row has gone since (a restart loses approvals)"
    return "not added by llmao; someone added it in LiteLLM"


def _orphan_actions(fleet, extra) -> list:
    """Stop tracking deletes an orphan. Approve, when a listing names a host not in fleet.hosts, intends it again."""
    hosts = {normalize_peer_ip(str(ip)) for ip in fleet.cfg.fleet.hosts}
    listed = match_rental(extra.host, None, fleet.vast_rentals, fleet.runpod_rentals) is not None
    actions = admin_actions(fleet, host=extra.host, approve=listed and extra.host not in hosts)
    reason = extra_refusal(extra) or ""
    ident = f"{extra.host}:{extra.port}"
    actions.append(
        _action(
            kind="remove",
            label="Stop tracking",
            host=extra.host,
            name=extra.model_name,
            port=str(extra.port),
            aria=f"Stop tracking {ident}",
            confirm=f"Stop tracking {ident}? This drops its LiteLLM route. The box keeps running.",
            post="/do-remove-deployment",
            enabled=ezt.boolean(reason == ""),
            reason=reason,
        )
    )
    return actions


def _hosting_label(ours: int, bought: int) -> str:
    """Where a model runs, from where its deployments run."""
    if ours and bought:
        return "Self-hosted and external"
    if ours:
        return "Self-hosted"
    return "External" if bought else "—"


def model_catalog_rows(fleet, catalog: list, *, admin: bool = False) -> list:
    """One Models-page row per catalog entry, with the user-facing roll-up.

    Context and concurrency come from the roll-up (minimum context across
    healthy deployments; concurrency summed over healthy self-hosted ones),
    so a catalog figure cannot overstate a degraded replica. For admins the
    row also carries the model's deployment rows and a replica summary.
    Kept out of models_page so a test can render models.ezt without a request.
    """
    rows = []
    for m in catalog:
        row = edict(m)
        status = fleet.model_rollup(row.model_name)
        rollup = status.rollup
        own = [d for d in fleet.deployments if d.model_name == row.model_name and not getattr(d, "litellm_only", False)]
        row.self_count = sum(1 for d in own if d.self_hosted)
        row.external_count = sum(1 for d in own if not d.self_hosted)
        row.hosting_label = _hosting_label(row.self_count, row.external_count)
        in_service = model_available_for(None, m) and model_in_service(rollup)
        if row.self_count:
            in_service = in_service and fleet.model_in_litellm(row.model_name)
        row.rollup = rollup
        row.rollup_label = _ROLLUP_LABEL.get(rollup, rollup)
        row.available = ezt.boolean(in_service)
        row.degraded = ezt.boolean(rollup == DEGRADED and in_service)
        row.unavailable = ezt.boolean(not in_service)
        row.context_window = f"{status.context_window:,}" if status.context_window else "—"
        if status.concurrency:
            n = status.concurrency
            row.concurrency = "1 long request at a time" if n == 1 else f"{n} long requests at once"
        else:
            row.concurrency = ""
        privacy = status.privacy
        row.private = ezt.boolean(privacy == PRIVATE)
        lead, rest = _PRIVACY_TEXT.get(privacy, ("", ""))
        row.privacy_lead = lead
        row.privacy_rest = rest
        row.privacy_key = privacy
        row.privacy_icon = _PRIVACY_ICON.get(privacy, "")
        row.privacy = f"{lead}. {rest}" if lead else ""
        row.chips = _modality_chips(m.get("modality") or "")
        if admin:
            row.deployments = [r for r in deployment_rows(fleet, admin=True) if r.model_name == row.model_name]
            row.replica_summary = _replica_summary(row.model_name, fleet)
        rows.append(row)
    # Usable first: available, then degraded, then the ones nobody can call.
    rows.sort(key=lambda r: (bool(r.unavailable), bool(r.degraded), (r.display_name or "").lower()))
    for i, row in enumerate(rows):
        row.index = i
    return rows


@APP.get("/models")
@asfquart.auth.require
@use_template(TEMPLATES / "models.ezt")
@page(FileNotFoundError, ValueError, OSError, title="Models")
async def models_page(result):
    """Gateway model inventory (public fields; supply path for site admins)."""
    admin = bool(result.is_site_admin)
    result.reveal_supply = admin
    catalog = ux_models(cfg=APP.cfg, reveal_supply=admin)
    result.models = model_catalog_rows(APP.fleet, catalog, admin=admin)
    result.attention = attention_items(APP.fleet) if admin else []
    result.approve_choices = _approve_choices(APP.cfg) if admin else []
    return result


def _approve_choices(cfg) -> list:
    """Self-hosted models an unknown box can be told to load."""
    choices = []
    for model in load_models(cfg=cfg):
        info = model.model_info
        if vllm_recipe(model) is None:
            continue
        choices.append(
            edict(model_name=str(model.model_name), display_name=str(info.get("display_name") or model.model_name))
        )
    return choices


def deployment_rows(fleet, *, admin: bool) -> list:
    """One row per intended deployment, admin fields gated on ``admin``.

    ``primary`` and ``menu`` are the actions the Deployments list shows: at
    most one button, the rest behind the "⋯" menu, and nothing that does not
    apply. ``diags`` is the second line, with empty fields left out.
    """
    rows = []
    cfg = fleet.cfg.fleet
    for dep in fleet.deployments:
        srv = dep.vllm
        if srv is not None:
            last_ok = srv.last_ok_display
            no_deployment = srv.state == HEALTHY and not dep.in_litellm
            serving = srv.state == HEALTHY and dep.in_litellm
            loading = srv.state == LOADING
            unhealthy = srv.state in (UNHEALTHY, STALLED, REJECTED, FOREIGN)
            awaiting = srv.state == AWAITING
            pending = False
            state = srv.state
            # Awaiting contact: the box has not fetched its config yet, so
            # there is nothing to load. Loading has a detail: vLLM is either
            # up with a 503 while the model loads, or not listening at all.
            detail = _deployment_reason(dep)
            listen = f"{srv.host}:{srv.listen_port}" if admin else ""
            public = (
                f"{srv.host}:{srv.public_port}" if admin and srv.public_port is not None else ("—" if admin else "")
            )
            host = srv.host if admin else ""
            config_ago = fleet.config_fetch_display(srv.host) if admin else ""
            # Measured KV cache against the served context window. A
            # max_model_len above the cache makes vLLM hang on a request that
            # needs the space rather than refuse at startup, so it reads as a
            # slow model rather than a misconfiguration.
            kv_cache = f"{srv.kv_cache_tokens:,}" if srv.kv_cache_tokens is not None else "—"
            served_len = srv.observed_max_model_len or srv.max_model_len
            context = f"{served_len:,}" if served_len else "—"
            oversized = srv.oversized
        else:
            last_ok = dep.litellm_health_display
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
        actions = admin_actions(fleet, dep) if admin else []
        usable = [a for a in actions if a.enabled]
        primary = [a for a in usable if a.is_add][:1]
        menu = [a for a in usable if not a.is_add]
        litellm_health = (
            "healthy" if dep.litellm_healthy is True else ("unhealthy" if dep.litellm_healthy is False else "—")
        )
        litellm_health_ago = dep.litellm_health_display if admin else ""
        skew_pills = [SKEW_PHRASE.get(n, n) for n in dep.skew] if admin else []
        diags = []
        if admin and state in (HEALTHY, UNHEALTHY):

            def ago(age: str) -> str:
                return f"{age} ago" if age and age not in ("—", "never") else ""

            # Each fact names its subject, then says one thing about it.
            vllm_fact = ""
            if srv is not None:
                vllm_fact = f"last healthy {ago(last_ok)}" if ago(last_ok) else "never healthy"
            litellm_fact = ""
            if litellm_health != "—":
                verdict = "check passed" if dep.litellm_healthy else "check failed"
                litellm_fact = f"{verdict} {ago(litellm_health_ago)}".strip()
            fields = [
                # The public address only when Vast maps it to another port.
                ("Public", public if srv is not None and public != listen else "", False),
                ("Context", context, False),
                ("KV cache", kv_cache, False),
                ("vLLM", vllm_fact, srv is not None and state == UNHEALTHY),
                ("LiteLLM", litellm_fact, dep.litellm_healthy is False),
                ("Config", f"served {ago(config_ago)}" if ago(config_ago) else "", False),
            ]
            diags = [edict(label=k, value=v, bad=ezt.boolean(bad)) for k, v, bad in fields if v and v != "—"]
        lifecycle = deployment_status(snapshot_for_status(dep, cfg), cfg).lifecycle
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
                last_ok=last_ok,
                config_ago=config_ago,
                skew="; ".join(SKEW_PHRASE.get(n, n) for n in dep.skew) if admin and dep.skew else "",
                kv_cache=kv_cache,
                context=context,
                oversized=ezt.boolean(oversized),
                in_litellm=ezt.boolean(dep.in_litellm),
                litellm_health=litellm_health,
                litellm_health_ago=litellm_health_ago,
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
                # Why an approved host's rows are here: not fleet.hosts, a vouch.
                flag=(admin and srv is not None and fleet.approval_display(srv.host)) or "",
                actions=actions,
                primary=primary,
                menu=menu,
                ident=_dep_ident(dep) if admin else "",
                where=_dep_where(dep) if admin else edict(label="", url="", unknown=ezt.boolean(False)),
                anchor=_dep_anchor(dep) if admin else "",
                external=ezt.boolean(not dep.self_hosted),
                attention=ezt.boolean(
                    admin and _needs_attention(lifecycle, dep.skew, unlisted=srv is not None and srv.unlisted)
                ),
                badge_class=_badge_class(state, routed=dep.in_litellm or srv is None),
                diags=diags,
                skew_pills=skew_pills,
            )
        )
    return rows


async def _done(ident, what: str, subject: str, sentence: str) -> None:
    """Tell the admin what they just did, and keep it for /admin's Recent work.

    The sentence is the record: what the admin is told is what other admins
    later read. Failing to keep it does not undo the action, so it is
    logged, not shown as an error.
    """
    await flash_success(sentence)
    try:
        await APP.actions.record(by=ident.uid, what=what, subject=subject, sentence=sentence)
    except BackendUnavailableError as e:
        _LOGGER.warning("could not keep the action %r: %s", sentence, e)


@APP.post("/do-add-deployment")
@asfquart.auth.require
async def do_add_deployment():
    """Site admin posts one self-hosted row. add_refusal is the gate."""
    ident = await current_identity(APP.cfg)
    if not ident.is_site_admin:
        await flash_danger("Only site admins can add a fleet deployment.")
        return _see_other("/models")
    form = await quart.request.form
    name = (form.get("name") or "").strip()
    host = (form.get("host") or "").strip()
    # Optional: one host can run the same model on two ports.
    port = (form.get("port") or "").strip()
    matches = _match_deployments(APP.fleet, name, host, port)
    if len(matches) != 1:
        await flash_danger(f"No deployment {name} on {host}:{port}." if port else f"No deployment {name} on {host}.")
        return _see_other("/models")
    reason = add_refusal(matches[0])
    if reason:
        await flash_danger(reason)
        return _see_other("/models")
    try:
        await APP.backend.deployments.add_deployment(matches[0])
    except BackendUnavailableError as e:
        await flash_danger(str(e))
        return _see_other("/models")
    ident_s = _dep_ident(matches[0])
    await _done(ident, "add_deployment", ident_s, f"Added {matches[0].name} on {ident_s} to LiteLLM.")
    return _see_other("/models")


@APP.post("/do-remove-deployment")
@asfquart.auth.require
async def do_remove_deployment():
    """Site admin drops one LiteLLM route: a self-hosted deployment (remove_refusal is the
    gate) or an orphan the skew pass reported (extra_refusal is)."""
    ident = await current_identity(APP.cfg)
    if not ident.is_site_admin:
        await flash_danger("Only site admins can remove a fleet deployment.")
        return _see_other("/models")
    form = await quart.request.form
    name = (form.get("name") or "").strip()
    host = (form.get("host") or "").strip()
    # Optional: one host can run the same model on two ports.
    port = (form.get("port") or "").strip()
    matches = _match_deployments(APP.fleet, name, host, port)
    extra = not matches and _match_extra(APP.fleet, name, host, port)
    if extra:
        return await _stop_tracking_extra(ident, extra)
    if len(matches) != 1:
        await flash_danger(f"No deployment {name} on {host}:{port}." if port else f"No deployment {name} on {host}.")
        return _see_other("/models")
    reason = remove_refusal(matches[0])
    if reason:
        await flash_danger(reason)
        return _see_other("/models")
    try:
        await APP.backend.deployments.delete_deployment(matches[0])
    except BackendUnavailableError as e:
        await flash_danger(str(e))
        return _see_other("/models")
    ident_s = _dep_ident(matches[0])
    await _done(ident, "remove_deployment", ident_s, f"Stopped tracking {matches[0].name} on {ident_s}.")
    return _see_other("/models")


async def _stop_tracking_extra(ident, extra):
    """Stop tracking an orphan: delete the LiteLLM route nothing intends."""
    reason = extra_refusal(extra)
    if reason:
        await flash_danger(reason)
        return _see_other("/models")
    try:
        await APP.backend.deployments.delete_extra(extra)
    except BackendUnavailableError as e:
        await flash_danger(str(e))
        return _see_other("/models")
    ident_s = f"{extra.host}:{extra.port}"
    await _done(
        ident,
        "remove_deployment",
        ident_s,
        f"Stopped tracking {extra.model_name} on {ident_s}; nothing in config intended it.",
    )
    return _see_other("/models")


def apply_box_approval(fleet, host: str, names: list[str]) -> tuple[bool, str]:
    """Remember the models for an unknown box. The testable core of /do-approve-box."""
    host = (host or "").strip()
    if not host:
        return False, "No host to approve."
    try:
        approval = fleet.approve_models(host, names)
    except ValueError as e:
        return False, str(e)
    return True, f"Approved {host} to load {', '.join(approval.models)}."


@APP.post("/do-approve-box")
@asfquart.auth.require
async def do_approve_box():
    """Site admin picks the models an unknown box loads on its next config fetch."""
    ident = await current_identity(APP.cfg)
    if not ident.is_site_admin:
        await flash_danger("Only site admins can approve a fleet box.")
        return _see_other("/models")
    form = await quart.request.form
    ok, message = apply_box_approval(APP.fleet, form.get("host"), form.getlist("model"))
    if ok:
        await _done(ident, "approve_box", (form.get("host") or "").strip(), message)
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
                name=r.project,
                href=f"/projects/{r.project}",
                is_steward=ezt.boolean(r.is_steward),
                spend=_money(r.spend),
                max_budget=_money(r.max_budget),
                remaining=_money(r.remaining),
                pct_label=pct_label,
                budget_duration=r.budget_duration,
                grantor=r.grantor,
            )
        )
    return out


@APP.get("/projects")
@asfquart.auth.require
@use_template(TEMPLATES / "projects.ezt")
@page(title="Projects")
async def projects_list(result):
    """Projects you belong to, with project-budget summary."""
    ident = await current_identity(APP.cfg)
    result.project_rows = _project_list_rows(await APP.seam.list_projects_for(ident))
    return result


@APP.get("/projects/<project>")
@asfquart.auth.require
@use_template(TEMPLATES / "project.ezt")
@page()
async def project_overview(result, project: str):
    """What a project is drawing, and who is drawing it.

    Two meters because they are two different things. The commercial bar
    fills toward a limit that blocks; the capacity bar toward one that slows.
    Giving them the same visual language would imply the same consequence.

    Service keys get their own row rather than being folded into whoever
    created them. Otherwise the member figures sum to less than the project
    total and the page looks broken -- and the pipeline is often the largest
    consumer, which is exactly what a PMC wants to see.
    """
    ident = await current_identity(APP.cfg)
    status = await APP.seam.team_status(ident, project)
    result.title = project
    result.project = project
    result.is_pmc = ezt.boolean(project in (ident.committees or []))

    cap = getattr(status, "token_cap", 0) or 0
    budget = status.max_budget or 0

    try:
        rows = await APP.seam.project_usage(ident, project)
    except BackendUnavailableError:
        rows = []

    tokens = sum(r.tokens for r in rows)
    spend = sum(r.spend for r in rows)

    result.tokens_h = _tokens_h(tokens)
    result.cap_h = _tokens_h(cap) if cap else None
    result.tokens_pct = min(100, int(tokens / cap * 100)) if cap else 0
    # A capacity estimate at the fleet rate. Shown beside the token count
    # rather than instead of it: the tokens are what constrains the project,
    # the dollars are what makes it comparable with commercial work.
    result.capacity_h = f"${tokens / 1_000_000 * CAPACITY_USD_PER_MTOK:,.2f}"

    result.spend_h = f"${spend:,.2f}"
    result.budget_h = f"${budget:,.0f}" if budget else None
    result.spend_pct = min(100, int(spend / budget * 100)) if budget else 0

    # An outstanding ask replaces the form: a second request while the first
    # is unanswered is noise for the admin and confusion for the PMC.
    try:
        mine = await APP.seam.project_requests(ident, project)
    except (AuthzError, BackendUnavailableError):
        mine = []
    pending = [r for r in mine if r.state == "pending" and r.kind == "capacity"]
    result.pending_request = _request_rows(pending)[0] if pending else None

    result.members = [
        edict(
            name=r.name,
            is_service=ezt.boolean(r.is_service),
            tokens_h=_tokens_h(r.tokens),
            spend_h=f"${r.spend:,.2f}",
            keys=r.key_count,
        )
        for r in rows
    ]
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
        secret=created.secret,
        purpose=created.info.purpose or "—",
        project=created.info.project,
        kind_label=kind_label,
        is_automation=ezt.boolean(created.info.is_automation),
        created_by=created.info.created_by or "",
        keys_back=keys_back,
        keys_create_another=keys_create_another,
    )
    html = _render("flash_key_created.ezt", data)
    await quart.flash(html, "raw")


@APP.get("/keys")
@asfquart.auth.require
@use_template(TEMPLATES / "keys.ezt")
@page(title="My Keys")
async def keys_list(result):
    """My Keys — the allowance, the keys drawing on it, and project draw.

    One page rather than two. The allowance is per PERSON: several free-tier
    keys share one million tokens, so a per-key "remaining" column would print
    the same figure on every row and imply each had its own budget. Per-key
    USED is the useful number anyway — it is how you find the key left running
    in a cron job.
    """
    ident = await current_identity(APP.cfg)
    keys = await APP.seam.list_my_keys(ident)
    result.keys = _key_rows(
        keys,
        after_path="/keys",
        used_by_token=await APP.seam.my_key_usage(ident),
    )
    result.has_project_keys = ezt.boolean(any(k.project not in PRETEND_PROJECTS for k in keys))

    # A page that fails because the allowance could not be read is worse than
    # one that renders without it: the keys are what the person came for.
    try:
        result.allowance = _allowance_row(await APP.seam.my_allowance(ident))
    except BackendUnavailableError:
        result.allowance = None

    try:
        result.projects = await APP.seam.my_project_usage(ident)
    except BackendUnavailableError:
        result.projects = []

    # One read, two views: the approvals are a filter of the same list.
    try:
        mine = await APP.seam.my_requests(ident)
    except BackendUnavailableError:
        mine = []

    try:
        result.approvals = [
            edict(
                id=a.id,
                purpose=a.purpose,
                tier=a.tier,
                project=a.project,
                # What this key will be allowed to do, said plainly. The
                # person about to create it has one question -- is this
                # what I asked for -- and the parameters are the answer.
                limits_h=(f"{_tokens_h(a.token_cap)} tokens/week" if a.token_cap else "tier defaults"),
                expires_h=(_when_h(a.expires_at.date()) if a.expires_at else ""),
            )
            for a in APP.seam.actionable_key_approvals(mine)
        ]
    except (BackendUnavailableError, AttributeError):
        result.approvals = []

    # Answers appear where the asking happened, so nobody has to find an
    # email to learn they were turned down.
    result.my_requests = _request_rows(mine)
    return result


def _allowance_row(a):
    """Template fields for the free-tier bar.

    Computed here rather than in the template because ezt has no arithmetic,
    and because the percentage, the warning threshold and the return date are
    all judgements rather than data.
    """
    pct = min(100, int(a.used() / a.cap * 100)) if a.cap else 0
    returns = a.returns()
    row = edict(
        cap_h=_tokens_h(a.cap),
        used_h=_tokens_h(a.used()),
        remaining_h=_tokens_h(a.remaining()),
        window_days=a.window_days,
        pct=pct,
        exhausted=ezt.boolean(a.exhausted()),
        # Warn before the wall rather than at it. Discovering a limit by
        # hitting it mid-job is the experience worth designing away.
        nearly=ezt.boolean(pct >= 80),
        # A capacity estimate at the fleet rate, so self-hosted work can be
        # compared with commercial spend. Not a bill.
        capacity_h=f"${a.used() / 1_000_000 * CAPACITY_USD_PER_MTOK:,.2f}",
        next_return=None,
        next_return_amount_h=None,
    )
    if returns:
        day, amount = returns[0]
        row.next_return = _when_h(day)
        row.next_return_amount_h = _tokens_h(amount)
    return row


# Fleet capacity estimate, dollars per million tokens. Derived from measured
# throughput at a 50% planning utilisation -- it exists so self-hosted and
# commercial work can appear in the same units, and is not an amount payable.
CAPACITY_USD_PER_MTOK = 0.32


def _tokens_h(n: int) -> str:
    """Human token count. 340k reads faster than 340,000 and this is a glance."""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def _when_h(day) -> str:
    """'tomorrow' / 'Tuesday' / a date, whichever is most useful.

    'Your tokens return 2026-09-23' is technically complete and makes the
    reader do arithmetic. The point of showing a return date at all is to turn
    a dead end into a plan.
    """
    import datetime as dt

    today = dt.datetime.now(dt.UTC).date()
    delta = (day - today).days
    if delta <= 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta < 7:
        return f"on {day.strftime('%A')}"
    return f"on {day.isoformat()}"


@APP.get("/usage")
@asfquart.auth.require
async def usage_moved():
    """Folded into My Keys. Redirect rather than 404 a bookmark."""
    return quart.redirect("/keys")


@APP.get("/admin")
@asfquart.auth.require
@use_template(TEMPLATES / "admin.ezt")
@page(title="Admin")
async def admin_index(result):
    """Everything that needs a site admin, on one page.

    What is waiting comes first and says how much is waiting: requests to
    decide, approvals nobody has used yet, and Work needed. Then what admins
    have done lately, then the settings pages. Each section is read on its
    own, so one source being down leaves the rest of the page.
    """
    if not result.is_site_admin:
        raise AuthzError("Admin is limited to site admins.")
    ident = await current_identity(APP.cfg)
    mine = quart.request.args.get("who") == "me"

    try:
        stored = await APP.backend.requests.find_all()
    except BackendUnavailableError:
        stored = []
    pending = sorted((r for r in stored if r.state == "pending"), key=lambda r: r.created_at or "")
    result.requests = _request_rows(pending)
    result.pending_count = len(pending) or None
    result.approvals = _open_approval_rows(stored)
    result.approval_count = len(result.approvals) or None

    try:
        skew = await APP.seam.work_needed(ident)
    except (AuthzError, BackendUnavailableError):
        skew = []
    result.skew = _skew_rows(skew)
    result.skew_count = len(result.skew) or None

    result.recent = await _recent_work(stored, only=ident.uid if mine else None)
    result.recent_days = RECENT_DAYS
    result.recent_mine = ezt.boolean(mine)

    try:
        result.litellm_running = await APP.seam.proxy_version()
    except BackendUnavailableError:
        result.litellm_running = None
    result.litellm_expected = pinned_litellm_version()
    result.litellm_matches = ezt.boolean(
        bool(result.litellm_running) and result.litellm_running == result.litellm_expected
    )
    return result


# How far back Recent work reaches.
RECENT_DAYS = 14

# Bootstrap icon for each kind of recent work.
_WORK_ICONS = {
    "approved": "bi-check-circle",
    "denied": "bi-x-circle",
    "used": "bi-key",
    "service_key": "bi-robot",
    "tier": "bi-sliders",
    "add_deployment": "bi-plus-circle",
    "remove_deployment": "bi-dash-circle",
    "approve_box": "bi-hdd-network",
    "admin_label": "bi-flag",  # logged before admin labels were removed; old rows still render
    "restrict_project": "bi-shield-lock",
    "revoke_key": "bi-slash-circle",
}


def _key_ask(r) -> str:
    """What a key request asks for, in words: 'one service key for tooling, "hadoop CI"'."""
    want = {**r.wanted, **(r.granted or {})}
    tier = want.get("tier") or "service"
    kind = "personal free-tier key" if tier == TIER_FREE else f"{tier.replace('_', ' ')} key"
    where = f" for {r.project}" if r.project else ""
    purpose = f', "{want["purpose"]}"' if want.get("purpose") else ""
    return f"one {kind}{where}{purpose}"


def _approval_means(r) -> str:
    """What saying yes to a pending request does, in words."""
    if r.kind == "capacity":
        return f"Sets {r.project}'s weekly allocation to {_tokens_h(r.wanted.get('token_cap') or 0)} tokens, at once."
    return (
        f"Lets {r.requester} create {_key_ask(r)}. Nothing is created until they do; "
        f"they have {APPROVAL_TTL_DAYS} days."
    )


def _open_approval_rows(stored) -> list:
    """Key approvals granted and not yet used: the rights still outstanding."""
    rows = []
    for r in stored:
        if r.kind != "key" or not r.is_actionable():
            continue
        exp = r.expires_at()
        days_left = (exp.date() - dt.datetime.now(dt.UTC).date()).days if exp else None
        rows.append(
            edict(
                requester=r.requester,
                ask=_key_ask(r),
                decided_by=r.decided_by,
                decided_on=(r.decided_at or "")[:10],
                expires_h=_when_h(exp.date()) if exp else "",
                soon=ezt.boolean(days_left is not None and days_left <= 3),
            )
        )
    return sorted(rows, key=lambda row: row.expires_h)


def _ago_h(stamp: str) -> str:
    """'5m ago', '3h ago', '2d ago' from an ISO stamp; '' if unreadable."""
    try:
        at = dt.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return ""
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.UTC)
    secs = max(0, int((dt.datetime.now(dt.UTC) - at).total_seconds()))
    if secs < 3600:
        return f"{max(1, secs // 60)}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _work(at: str, by: str, kind: str, sentence: str) -> edict:
    return edict(at=at or "", by=by or "", sentence=sentence, icon=_WORK_ICONS.get(kind, "bi-dot"), ago=_ago_h(at))


async def _recent_work(stored, *, only: str | None = None) -> list:
    """What admins did lately, newest first, from every record that keeps it.

    Request decisions and approvals used come from the requests; service
    keys from their creator on the key; tier changes from the tier; the
    rest from the action log. Each source is read on its own, so one being
    down thins the list rather than emptying the page.
    """
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=RECENT_DAYS)).isoformat(timespec="seconds")
    work = []
    for r in stored:
        what = "capacity" if r.kind == "capacity" else _key_ask(r)
        subject = r.project if r.kind == "capacity" else r.requester
        if r.decided_at:
            if r.state == "denied":
                work.append(_work(r.decided_at, r.decided_by, "denied", f"Denied {subject}'s request for {what}."))
            elif r.kind == "capacity":
                cap = _tokens_h((r.granted or r.wanted).get("token_cap") or 0)
                work.append(
                    _work(r.decided_at, r.decided_by, "approved", f"Raised {r.project}'s weekly allocation to {cap}.")
                )
            else:
                work.append(_work(r.decided_at, r.decided_by, "approved", f"Approved {r.requester} to create {what}."))
        if r.consumed_at:
            work.append(
                _work(r.consumed_at, r.requester, "used", f"{r.requester} used their approval and created {what}.")
            )
    try:
        for k in await APP.backend.keys.find(size=200):
            if k.is_automation and k.created_by and k.created_at:
                named = f' "{k.purpose}"' if k.purpose else ""
                work.append(
                    _work(k.created_at, k.created_by, "service_key", f"Created {k.project}'s service key{named}.")
                )
    except BackendUnavailableError:
        pass
    for tier in TIERS:
        try:
            change = await APP.backend.tiers.last_change(tier)
        except BackendUnavailableError:
            change = None
        if change:
            label = "free tier" if tier == TIER_FREE else f"{tier} tier"
            work.append(_work(change.at, change.by, "tier", f"Changed the {label}'s limits."))
    try:
        for a in await APP.actions.recent(RECENT_DAYS):
            work.append(_work(a.at, a.by, a.what, a.sentence))
    except BackendUnavailableError:
        pass
    work = [w for w in work if w.at >= since and (only is None or w.by == only)]
    return sorted(work, key=lambda w: w.at, reverse=True)


def _stamp(at) -> str:
    """LiteLLM's own stamp on a memory row, to the minute."""
    at = str(at or "")
    return f"{at[:16].replace('T', ' ')} UTC" if at else ""


# What a site admin can press to deal with each kind of skew: (where the
# form posts, what the button says). A kind with no entry has no button yet
# and is put right some other way.
_SKEW_ACTIONS = {
    "project": ("/do-restrict-project", "Restrict"),
}


def _skew_rows(entries) -> list:
    """Template rows for the Admin page's "Work needed"."""
    return [
        edict(
            what=n.get("what") or "",
            subject=n.get("subject") or "",
            action=_SKEW_ACTIONS.get(n.get("kind"), ("", ""))[0],
            action_label=_SKEW_ACTIONS.get(n.get("kind"), ("", ""))[1],
            # How long it has been wrong. An unchanged row is not rewritten,
            # so its last write says nothing about when it was last checked.
            since=_stamp(n.get("first_seen")),
        )
        for n in entries
    ]


def _request_rows(reqs) -> list:
    """Template rows for a request list.

    `wanted_h` and `granted_h` are rendered here because what a request is
    asking for depends on its kind, and ezt has no way to branch on that.
    """
    out = []
    for r in reqs:
        if r.kind == "capacity":
            wanted = _tokens_h(r.wanted.get("token_cap") or 0)
            granted = _tokens_h(r.granted.get("token_cap") or 0) if r.granted else ""
            what = "capacity"
        else:
            wanted = r.wanted.get("purpose") or "?"
            granted = r.granted.get("purpose") or "" if r.granted else ""
            what = "key"
        exp = r.expires_at()
        out.append(
            edict(
                id=r.id,
                kind=r.kind,
                what=what,
                requester=r.requester,
                project=r.project or "",
                reason=r.reason,
                wanted_h=wanted,
                granted_h=granted,
                tier=r.wanted.get("tier") or "",
                state=r.state,
                is_pending=ezt.boolean(r.state == "pending"),
                is_approved=ezt.boolean(r.state == "approved"),
                is_denied=ezt.boolean(r.state == "denied"),
                is_capacity=ezt.boolean(r.kind == "capacity"),
                actionable=ezt.boolean(r.is_actionable()),
                # Only worth showing the ask when it differs. "30M, asked
                # 30M" is noise on every approved-as-requested row.
                was_adjusted=ezt.boolean(bool(r.granted) and r.granted != r.wanted),
                decision_reason=r.decision_reason,
                decided_by=r.decided_by,
                means=_approval_means(r) if r.state == "pending" else "",
                # The question a requester actually has: how long have I
                # got to act on this.
                expires_h=_when_h(exp.date()) if exp else "",
                created_at=(r.created_at or "")[:10],
            )
        )
    return out


# The periods /admin/capacity offers, in days.
CAPACITY_PERIODS = (7, 30, 90)


@APP.get("/admin/capacity")
@asfquart.auth.require
@use_template(TEMPLATES / "capacity.ezt")
@page(title="Capacity")
async def admin_capacity(result):
    """What the fleet did and cost, and what the providers we pay were paid.

    For deciding whether to add a box or return one, and for budgeting.
    Built from the hourly capacity rows and LiteLLM's daily activity; each
    half is read on its own, so one being down leaves the other.
    """
    if not result.is_site_admin:
        raise AuthzError("Capacity is limited to site admins.")
    try:
        days = int(quart.request.args.get("days") or 30)
    except ValueError:
        days = 30
    if days not in CAPACITY_PERIODS:
        days = 30
    today = dt.datetime.now(dt.UTC).date()
    result.period_days = days
    result.periods = [edict(days=d, active=ezt.boolean(d == days)) for d in CAPACITY_PERIODS]

    try:
        rows = await APP.capacity.read(days)
        result.capacity_down = ezt.boolean(False)
    except BackendUnavailableError:
        rows = []
        result.capacity_down = ezt.boolean(True)
    fleet = summarise_capacity(rows, today=today, period_days=days)
    result.models = [_capacity_model_row(m) for m in fleet.models]
    result.fleet_cost_per_day = _money(fleet.cost_per_day)
    result.fleet_tokens_per_day = _tokens_h(int(fleet.tokens_per_day))
    result.unpriced_hours = fleet.unpriced_hours or None

    try:
        spent = await APP.backend.metering.provider_spend(days_to_read(days, today))
        result.spend_down = ezt.boolean(False)
    except BackendUnavailableError:
        spent = []
        result.spend_down = ezt.boolean(True)
    providers = summarise_spend(spent, today=today, period_days=days)
    result.providers = [_spend_provider_row(p) for p in providers]
    result.spend_month_to_date = _money(sum(p.month_to_date_usd for p in providers))
    result.spend_projected = _money(sum(p.projected_month_usd for p in providers))
    return result


def _pct(fraction: float) -> int:
    return max(0, min(100, round(fraction * 100)))


def _bars(series: list, fmt) -> list:
    """A day-by-day series as bar heights, tallest 100: [{height, title}].

    A bar too small to see still gets a sliver, so a quiet day reads as
    quiet rather than as missing.
    """
    top = max((p.value for p in series), default=0)
    return [
        edict(
            height=max(2, round(p.value / top * 100)) if top else 2,
            title=f"{p.day}: {fmt(p.value)}",
        )
        for p in series
    ]


def _capacity_model_row(m) -> edict:
    # How long requests sat in vLLM's queue before it started on them. Each
    # hourly row keeps the 95th percentile of that hour (95% of requests
    # waited no longer); this is the worst such hour in the period, the one
    # that says whether the model ever ran short of room.
    #
    # vLLM records queue time in a histogram whose smallest bucket is 0.3s,
    # so it cannot tell 0.01s from 0.29s. A value of 0.3 therefore means
    # "under 0.3s", which for a person reading the page is "no real wait".
    # None means no request queued at all in the period.
    worst = m.queue_p95_worst_s
    if worst is None:
        queue_p95 = "—"
    elif worst <= 0.3:
        queue_p95 = "<0.3s"
    else:
        queue_p95 = f"{worst:g}s"

    return edict(
        model_name=m.model_name,
        cost_per_day=_money(m.cost_per_day),
        tokens_per_day=_tokens_h(int(m.tokens_per_day)),
        usd_per_mtok=_money(m.usd_per_mtok) if m.usd_per_mtok is not None else "—",
        busy=_pct(m.busy),
        busy_hot=ezt.boolean(m.busy >= 0.8),
        waiting=_pct(m.waiting),
        waiting_hot=ezt.boolean(m.waiting >= 0.05),
        kv_peak=_pct(m.kv_peak),
        kv_hot=ezt.boolean(m.kv_peak >= 0.9),
        waiting_peak=m.waiting_peak,
        preemptions=m.preemptions,
        queue_p95=queue_p95,
        prompt_tokens=_tokens_h(m.prompt_tokens),
        generation_tokens=_tokens_h(m.generation_tokens),
        requests=f"{m.requests:,}",
        hours=m.hours,
        unpriced_hours=m.unpriced_hours or None,
        token_bars=_bars(m.daily_tokens, lambda v: f"{_tokens_h(int(v))} tokens"),
        servers=[
            edict(
                server=srv.server,
                usd_per_hour=f"${srv.usd_per_hour:.3f}/hr" if srv.usd_per_hour is not None else "unpriced",
                busy=_pct(srv.busy),
                waiting=_pct(srv.waiting),
                kv_peak=_pct(srv.kv_peak),
                tokens=_tokens_h(srv.tokens),
            )
            for srv in m.servers
        ],
    )


def _spend_provider_row(p) -> edict:
    projected = p.projected_month_usd
    return edict(
        provider=p.provider,
        period=_money(p.period_usd),
        today=_money(p.today_usd),
        month_to_date=_money(p.month_to_date_usd),
        projected=_money(projected),
        # How far into the projected month the spend so far is.
        month_pct=_pct(p.month_to_date_usd / projected) if projected else 0,
        models=[edict(name=x.name, usd=_money(x.usd)) for x in p.models],
        projects=[edict(name=x.name, usd=_money(x.usd)) for x in p.projects],
        spend_bars=_bars([edict(day=d.day, value=d.usd) for d in p.daily], _money),
    )


@APP.get("/admin/tiers")
@asfquart.auth.require
@use_template(TEMPLATES / "tiers.ezt")
@page(title="Tiers")
async def admin_tiers(result):
    """Allowances and rate limits per tier."""
    ident = await current_identity(APP.cfg)
    result.tiers = [
        edict(
            tier=t["tier"],
            label=t["tier"].replace("_", " "),
            rpm_limit=t.get("rpm_limit") or "",
            tpm_limit=t.get("tpm_limit") or "",
            max_parallel_requests=t.get("max_parallel_requests") or "",
            # Only the free tier meters tokens. A project key draws on the
            # project's allocation and a service key on its own, so a cap
            # here would be a third ceiling nobody asked for.
            has_cap=ezt.boolean(t.get("token_cap") is not None),
            token_cap=t.get("token_cap") or "",
            token_window_days=t.get("token_window_days") or "",
        )
        for t in await APP.seam.tiers(ident)
    ]
    return result


@APP.post("/do-set-tier")
@asfquart.auth.require
async def do_set_tier():
    """Change one tier.

    Empty fields are left alone rather than cleared: a form that blanked
    everything it did not mention would uncap a tier by omission, which is
    the opposite of what this page is for.
    """
    form = await quart.request.form

    def _int(name):
        raw = (form.get(name) or "").replace(",", "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    try:
        ident = await current_identity(APP.cfg)
        await APP.seam.set_tier(
            ident,
            (form.get("tier") or "").strip(),
            rpm_limit=_int("rpm_limit"),
            tpm_limit=_int("tpm_limit"),
            max_parallel_requests=_int("max_parallel_requests"),
            token_cap=_int("token_cap"),
            token_window_days=_int("token_window_days"),
        )
        await flash_success(
            "Tier updated. The token cap applies immediately; rate limits apply to keys issued from now on."
        )
    except (AuthzError, BackendUnavailableError, ValueError) as e:
        await flash_danger(str(e))
    return _see_other("/admin/tiers")


@APP.post("/do-request-capacity")
@asfquart.auth.require
async def do_request_capacity():
    form = await quart.request.form
    project = (form.get("project") or "").strip()
    try:
        cap = int((form.get("token_cap") or "0").replace(",", "").strip() or 0)
    except ValueError:
        cap = 0
    try:
        ident = await current_identity(APP.cfg)
        await APP.seam.request_capacity(ident, project, token_cap=cap, reason=form.get("reason") or "")
        await flash_success("Request sent. You will see the answer on this page.")
    except (AuthzError, BackendUnavailableError, ValueError) as e:
        await flash_danger(str(e))
    return _see_other(_safe_after_path(form.get("after_path"), f"/projects/{project}"))


@APP.post("/do-request-key")
@asfquart.auth.require
async def do_request_key():
    form = await quart.request.form
    try:
        ident = await current_identity(APP.cfg)
        await APP.seam.request_key(
            ident,
            purpose=form.get("purpose") or "",
            tier=form.get("tier") or "",
            reason=form.get("reason") or "",
            project=(form.get("project") or "").strip() or None,
        )
        await flash_success("Request sent. If approved you will create the key here.")
    except (AuthzError, BackendUnavailableError, ValueError) as e:
        await flash_danger(str(e))
    return _see_other(_safe_after_path(form.get("after_path"), "/keys"))


@APP.post("/do-decide-request")
@asfquart.auth.require
async def do_decide_request():
    """Approve, adjust or deny.

    An adjusted capacity figure arrives in the same form as the decision, so
    approving as-asked and approving-but-smaller are one action rather than
    two screens.
    """
    form = await quart.request.form
    granted = None
    raw = (form.get("granted_token_cap") or "").replace(",", "").strip()
    if raw:
        try:
            granted = {"token_cap": int(raw)}
        except ValueError:
            granted = None
    try:
        ident = await current_identity(APP.cfg)
        req = await APP.seam.decide_request(
            ident,
            (form.get("request_id") or "").strip(),
            approve=(form.get("decision") == "approve"),
            reason=form.get("reason") or "",
            granted=granted,
        )
        await flash_success(_decided_h(req))
    except (AuthzError, BackendUnavailableError, ValueError) as e:
        await flash_danger(str(e))
    return _see_other("/admin#requests")


def _decided_h(r) -> str:
    """What the admin just did, in words, so a yes to the wrong thing is caught at once."""
    if r.state == "denied":
        subject = r.project if r.kind == "capacity" else r.requester
        return f"Denied {subject}'s request. They will see your reason."
    if r.kind == "capacity":
        cap = _tokens_h(r.granted.get("token_cap") or 0)
        return f"Raised {r.project}'s weekly allocation to {cap}. It applies now."
    exp = r.expires_at()
    until = f" until {exp.date().isoformat()}" if exp else ""
    return f"Approved: {r.requester} may create {_key_ask(r)}{until}."


@APP.get("/keys/other")
@asfquart.auth.require
@use_template(TEMPLATES / "keys_other.ezt")
@page(title="Service keys")
async def keys_other_list(result):
    """Service keys — every key with no human at the keyboard.

    Reached from Admin rather than the committer nav: a committer seeing
    other people's automation keys learned nothing and could act on none of
    it. The URL is unchanged so existing links keep working.
    """
    if not result.can_create_automation:
        raise AuthzError("Service keys are limited to PMC members and site admins.")
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
@use_template(TEMPLATES / "key_create.ezt")
@page(title="Create personal key")
async def keys_new_form(result):
    model = (quart.request.args.get("model") or "").strip()
    result.for_model = model
    result.has_for_model = ezt.boolean(bool(model))
    # Nothing to choose between, so do not make them choose.
    if not result.form_project and len(result.projects) == 1:
        result.form_project = result.projects[0].name
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


@APP.post("/do-create-approved-key")
@asfquart.auth.require
async def do_create_approved_key():
    """Exercise an approval.

    The admin approved the RIGHT to create this key, not the key itself. It
    is created here, by the person who asked, and the secret is revealed to
    them once -- so it never sits in a message anywhere.

    The form carries only the approval id. Every parameter comes from the
    approval, because a form that could name its own tier would make the
    approval decorative.
    """
    form = await quart.request.form
    try:
        ident = await current_identity(APP.cfg)
        created = await APP.seam.create_approved_key(ident, (form.get("approval_id") or "").strip())
    except (AuthzError, BackendUnavailableError, ValueError) as e:
        await flash_danger(str(e))
        return _see_other("/keys")
    await _flash_key_created(
        created,
        kind_label="Approved",
        keys_back="/keys",
        keys_create_another="/keys",
    )
    return _see_other("/keys")


@APP.get("/keys/other/new")
@asfquart.auth.require
@use_template(TEMPLATES / "key_create_other.ezt")
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


@APP.post("/do-restrict-project")
@asfquart.auth.require
async def do_restrict_project():
    """A site admin puts a pretend project back to our own boxes only."""
    form = await quart.request.form
    project = (form.get("subject") or "").strip()
    try:
        ident = await current_identity(APP.cfg)
        await APP.seam.restrict_pretend_project(ident, project)
        await _done(ident, "restrict_project", project, f"Restricted {project} to our own boxes.")
    except (AuthzError, BackendUnavailableError) as e:
        await flash_danger(str(e))
    return _see_other("/admin")


@APP.post("/do-acknowledge")
@asfquart.auth.require
async def do_acknowledge():
    """Mark one log entry seen. Whose it is to clear is the seam's to decide.

    The button is on every page, so this goes back to whichever one it was
    pressed on.
    """
    form = await quart.request.form
    try:
        await APP.seam.acknowledge(await current_identity(APP.cfg), form.get("key") or "")
    except (AuthzError, BackendUnavailableError) as e:
        await flash_danger(str(e))
    return _see_other(_safe_back())


@APP.post("/do-revoke-key")
@asfquart.auth.require
async def do_revoke_key():
    form = await quart.request.form
    token_id = (form.get("token_id") or form.get("token") or "").strip()
    after_path = _safe_after_path(form.get("after_path"))
    try:
        ident = await current_identity(APP.cfg)
        seam = APP.seam
        key = await seam.revoke_key(ident, token_id)
        if key.user == ident.uid:
            # Someone tidying their own keys is not admin work.
            await flash_success("Key revoked.")
        else:
            whose = f"{key.project}'s service key" if key.is_automation else f"{key.user}'s key"
            named = f" '{key.purpose}'" if key.purpose else ""
            await _done(ident, "revoke_key", key.project or key.user or "", f"Revoked {whose}{named}.")
    except (AuthzError, BackendUnavailableError) as e:
        await flash_danger(str(e))
    return _see_other(after_path)


@APP.get("/static/<path:filename>")
async def serve_static(filename: str):
    return await quart.send_from_directory(STATICDIR, filename)


@APP.get("/favicon.ico")
async def serve_favicon():
    return await quart.send_from_directory(STATICDIR, "favicon.ico")

"""llmao control plane — asfquart app factory and routes.

Routes:
  GET  /                         status page (public)
  GET  /healthz                  liveness (public)
  GET  /v1/projects/<p>/usage    activity (authed; PMC admin of project)
  GET  /v1/projects/<p>/budget   budget + spend (authed; project member)

Always built with ``asfquart.construct`` (OAuth at /auth, session cookies,
optional token_handler). Completions are not proxied here — clients use
LiteLLM virtual keys against the proxy directly.
"""
from __future__ import annotations

from typing import Optional

import asfquart
import asfquart.auth
from asfquart.auth import Requirements as R
from quart import jsonify, Response

from .auth import current_identity, make_token_handler
from .config import Settings
from .litellm_client import make_backend
from .seam import AuthzError, Seam
from .store import StateStore
from .portal import render_index


def create_app(
    *,
    app_dir: Optional[str] = None,
    cfg_file: Optional[str] = None,
    token_file: Optional[str] = "apptoken.txt",
):
    """Construct the asfquart app and register routes.

    ``app_dir`` defaults to the process cwd (asfquart default). ``main.py``
    passes the repo root so ``config.yaml`` and ``apptoken.txt`` live there.
    """
    # Avoid a second stack of OIDC defaults; pin classic oauth.apache.org URLs
    # (same pattern as Apache STeVe).
    import asfquart.generics

    asfquart.generics.OAUTH_URL_INIT = (
        "https://oauth.apache.org/auth?state=%s&redirect_uri=%s"
    )
    asfquart.generics.OAUTH_URL_CALLBACK = "https://oauth.apache.org/token?code=%s"

    app = asfquart.construct(
        "llmao",
        app_dir=app_dir,
        cfg_file=cfg_file,
        token_file=token_file,
        oauth=True,
        force_login=True,
    )

    s = Settings.from_cfg(app.cfg)
    store = StateStore(s.state_path)
    backend = make_backend(s, store)
    seam = Seam(s, backend)
    app.token_handler = make_token_handler(s)

    app.config["LLMAO_SETTINGS"] = s
    app.config["LLMAO_SEAM"] = seam

    def _err(status: int, message: str) -> Response:
        resp = jsonify({"error": {"message": message, "type": "llmao_error", "code": status}})
        resp.status_code = status
        return resp

    @app.errorhandler(500)
    async def _on_500(exc):
        from quart import request as _req
        if _req.path.startswith("/v1/"):
            return _err(500, "internal error in control plane; check the server log")
        return exc

    # -- public -----------------------------------------------------------

    @app.route("/")
    async def index():
        ident = await current_identity(s)
        return Response(render_index(s, ident), content_type="text/html")

    @app.route("/healthz")
    async def healthz():
        return jsonify({"status": "ok", "llm_mode": s.litellm_mode})

    # -- authenticated control plane --------------------------------------

    @app.route("/v1/projects/<project>/usage")
    @asfquart.auth.require
    async def project_usage(project: str):
        ident = await current_identity(s)
        # require guarantees a session; identity mapping should always succeed.
        assert ident is not None
        try:
            rows = seam.project_activity(ident, project)
        except AuthzError as e:
            return _err(403, str(e))
        total = round(sum(r.get("cost_usd", 0.0) for r in rows), 6)
        return jsonify({"project": project, "entries": rows, "total_cost_usd": total, "count": len(rows)})

    @app.route("/v1/projects/<project>/budget")
    @asfquart.auth.require({R.committer})
    async def project_budget(project: str):
        ident = await current_identity(s)
        assert ident is not None
        try:
            seam.require_member(ident, project)
        except AuthzError as e:
            return _err(403, str(e))
        info = seam.team_status(project)
        if info is None:
            return jsonify({"project": project, "provisioned": False})
        return jsonify({
            "project": project, "provisioned": True,
            "max_budget_usd": info.max_budget, "spend_usd": info.spend,
            "remaining_usd": round(max(0.0, info.max_budget - info.spend), 6),
        })

    return app

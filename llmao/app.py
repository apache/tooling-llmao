"""llmao control plane — app factory and routes.

Routes:
  GET  /                      minimal status page
  GET  /healthz               liveness
  GET  /auth/dev/login        dev-stub login form (dev mode only)
  POST /auth/dev/login        establish a dev session
  GET  /auth/logout           clear session
  GET  /v1/projects/<p>/usage per-project activity (PMC admins only)
  GET  /v1/projects/<p>/budget team budget + spend

In asf mode the app is built via asfquart.construct() so it inherits the
OAuth gateway at /auth, JWT/PAT support, and LDAP-backed sessions. In dev
mode it's a plain Quart app with a stub login. Routes are identical.

This process does not proxy chat completions. Clients use LiteLLM virtual
keys against the proxy directly.
"""
from __future__ import annotations

from typing import Optional

from quart import Quart, jsonify, redirect, request, Response

from .auth import current_identity, dev_login, dev_logout
from .config import Settings, settings as default_settings
from .litellm_client import make_backend
from .seam import AuthzError, Identity, Seam
from .store import StateStore
from .portal import render_index, render_dev_login


def create_app(settings: Optional[Settings] = None) -> Quart:
    s = settings or default_settings
    store = StateStore(s.state_path)
    backend = make_backend(s, store)
    seam = Seam(s, backend)

    if s.is_dev_auth:
        app = Quart(__name__)
        app.secret_key = s.app_secret
    else:
        # Production: inherit OAuth gateway (/auth), PAT support, LDAP sessions.
        import asfquart
        from .auth import make_token_handler
        app = asfquart.construct("llmao", oauth=True, force_login=False)
        app.token_handler = make_token_handler(s)

    app.config["LLMAO_SETTINGS"] = s
    app.config["LLMAO_SEAM"] = seam

    # -- helpers ----------------------------------------------------------

    async def _identity() -> Optional[Identity]:
        return await current_identity(s)

    def _err(status: int, message: str) -> Response:
        resp = jsonify({"error": {"message": message, "type": "llmao_error", "code": status}})
        resp.status_code = status
        return resp

    # Any unhandled exception on an API route should still be JSON.
    @app.errorhandler(500)
    async def _on_500(exc):
        from quart import request as _req
        if _req.path.startswith("/v1/"):
            return _err(500, "internal error in control plane; check the server log")
        return exc

    # -- status -----------------------------------------------------------

    @app.route("/")
    async def index():
        ident = await _identity()
        return Response(render_index(s, ident), content_type="text/html")

    @app.route("/healthz")
    async def healthz():
        return jsonify({"status": "ok", "auth_mode": s.auth_mode, "llm_mode": s.litellm_mode})

    # -- dev auth ---------------------------------------------------------

    @app.route("/auth/dev/login", methods=["GET"])
    async def dev_login_form():
        if not s.is_dev_auth:
            return _err(404, "dev login disabled in asf mode")
        return Response(render_dev_login(), content_type="text/html")

    @app.route("/auth/dev/login", methods=["POST"])
    async def dev_login_submit():
        if not s.is_dev_auth:
            return _err(404, "dev login disabled in asf mode")
        form = await request.form
        uid = (form.get("uid") or "").strip()
        if not uid:
            return _err(400, "uid required")
        projects = [p.strip() for p in (form.get("projects") or "").split(",") if p.strip()]
        committees = [c.strip() for c in (form.get("committees") or "").split(",") if c.strip()]
        dev_login(uid, projects, committees)
        return redirect("/")

    @app.route("/auth/logout")
    async def logout():
        if s.is_dev_auth:
            dev_logout()
        return redirect("/")

    # -- activity + budget ------------------------------------------------

    @app.route("/v1/projects/<project>/usage")
    async def project_usage(project: str):
        ident = await _identity()
        if ident is None:
            return _err(401, "authentication required")
        try:
            rows = seam.project_activity(ident, project)
        except AuthzError as e:
            return _err(403, str(e))
        total = round(sum(r.get("cost_usd", 0.0) for r in rows), 6)
        return jsonify({"project": project, "entries": rows, "total_cost_usd": total, "count": len(rows)})

    @app.route("/v1/projects/<project>/budget")
    async def project_budget(project: str):
        ident = await _identity()
        if ident is None:
            return _err(401, "authentication required")
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


def main() -> None:
    s = default_settings
    app = create_app(s)
    app.run(host=s.host, port=s.port)


if __name__ == "__main__":
    main()

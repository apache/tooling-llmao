#!/usr/bin/env -S uv run --script
"""Standalone / ASGI entrypoint for llmao (Apache STeVe-style).

Loads config.yaml next to this file, serves with optional TLS from certs/,
and uses asfquart OAuth so redirect URIs work with localhost.apache.org.

  cp config.yaml.example config.yaml   # then edit secrets
  # generate PEMs under certs/ — see certs/README.md
  uv run python main.py

  # ASGI (e.g. Hypercorn):
  #   uv run python -m hypercorn main:llmao_app
"""
from __future__ import annotations

import logging
import pathlib
import sys

_LOGGER = logging.getLogger(__name__)
DATE_FORMAT = "%m/%d %H:%M"

THIS_DIR = pathlib.Path(__file__).resolve().parent
CERTS_DIR = THIS_DIR / "certs"

# Populated by run_asgi() for Hypercorn: ``main:llmao_app``
llmao_app = None


def create_app():
    """Create the asfquart app; pages/api register routes on import."""
    if str(THIS_DIR) not in sys.path:
        sys.path.insert(0, str(THIS_DIR))

    import asfquart
    import asfquart.generics

    # Pin classic oauth.apache.org URLs (same pattern as Apache STeVe).
    asfquart.generics.OAUTH_URL_INIT = (
        "https://oauth.apache.org/auth?state=%s&redirect_uri=%s"
    )
    asfquart.generics.OAUTH_URL_CALLBACK = "https://oauth.apache.org/token?code=%s"

    app = asfquart.construct(
        "llmao",
        app_dir=str(THIS_DIR),
        static_folder=None,  # /static served from pages.py
        oauth=True,
        force_login=True,
    )

    from llmao.auth import make_token_handler
    from llmao.config import Settings
    from llmao.litellm_client import make_backend
    from llmao.seam import Seam
    from llmao.store import StateStore

    s = Settings.from_cfg(app.cfg)
    store = StateStore(s.state_path)
    backend = make_backend(s, store)
    seam = Seam(s, backend)
    app.token_handler = make_token_handler(s)
    app.config["LLMAO_SETTINGS"] = s
    app.config["LLMAO_SEAM"] = seam

    from quart import jsonify, request as quart_request

    @app.errorhandler(500)
    async def _on_500(exc):
        if quart_request.path.startswith("/v1/"):
            resp = jsonify({
                "error": {
                    "message": "internal error in gateway; check the server log",
                    "type": "llmao_error",
                    "code": 500,
                }
            })
            resp.status_code = 500
            return resp
        return exc

    # Register routes (decorators bind to asfquart.APP).
    import pages  # noqa: F401
    import api  # noqa: F401

    return app


def run_standalone() -> None:
    """Run as a standalone server (asfquart runx + optional TLS)."""
    logging.basicConfig(
        level=logging.DEBUG,
        style="{",
        format="[{asctime}|{levelname}|{name}] {message}",
        datefmt=DATE_FORMAT,
    )
    logging.getLogger("selector_events").setLevel(logging.INFO)
    logging.getLogger("hpack").setLevel(logging.INFO)
    logging.getLogger("sslproto").setLevel(logging.INFO)
    logging.getLogger("asyncio").setLevel(logging.INFO)

    _LOGGER.info(" ** Run-mode: Standalone")

    if not (THIS_DIR / "config.yaml").is_file():
        _LOGGER.error(
            "Missing config.yaml next to main.py. "
            "Copy config.yaml.example to config.yaml and edit secrets."
        )
        sys.exit(1)

    app = create_app()

    kwargs = {}
    server = getattr(app.cfg, "server", None) or {}
    port = int(
        getattr(server, "port", None)
        or (server.get("port") if hasattr(server, "get") else None)
        or 8443
    )

    certfile = getattr(server, "certfile", None) if server is not None else None
    keyfile = getattr(server, "keyfile", None) if server is not None else None
    if isinstance(server, dict):
        certfile = server.get("certfile") or certfile
        keyfile = server.get("keyfile") or keyfile

    extra_files = set()
    if certfile and keyfile:
        cert_path = pathlib.Path(certfile)
        key_path = pathlib.Path(keyfile)
        if not cert_path.is_absolute():
            cert_path = CERTS_DIR / cert_path
        if not key_path.is_absolute():
            key_path = CERTS_DIR / key_path
        kwargs["certfile"] = str(cert_path)
        kwargs["keyfile"] = str(key_path)
        extra_files.update((cert_path, key_path))
        _LOGGER.info("TLS enabled: cert=%s key=%s", cert_path, key_path)
    else:
        _LOGGER.info("TLS disabled (no server.certfile/keyfile); plain HTTP")

    extra_files.add(THIS_DIR / "config.yaml")
    app.runx(port=port, extra_files=extra_files, **kwargs)


def run_asgi() -> None:
    """Run as an ASGI process (e.g. Hypercorn imports main:llmao_app)."""
    # NOTE: no-op if Hypercorn has set up the root logger.
    logging.basicConfig(
        level=logging.DEBUG,
        style="{",
        format="[{asctime}|{levelname}|{name}] {message}",
        datefmt=DATE_FORMAT,
    )
    logging.getLogger("watchfiles.main").setLevel(logging.INFO)
    _LOGGER.setLevel(logging.DEBUG)

    _LOGGER.info(" ** Run-mode: ASGI")

    global llmao_app
    llmao_app = create_app()


if __name__ == "__main__":
    # $ uv run python main.py
    run_standalone()
else:
    # Using Hypercorn:
    #
    #   $ uv run python -m hypercorn main:llmao_app
    #
    # NOTE: without our extended shutdown_trigger, we cannot reload
    # or restart on .py changes, extra_files changes, or respond to
    # SIGUSR2 to restart. Hypercorn will respond to SIGTERM/SIGINT
    # and shutdown.
    run_asgi()

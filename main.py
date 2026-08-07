#!/usr/bin/env -S uv run --script
"""Standalone entrypoint for llmao (Apache STeVe-style).

Loads config.yaml next to this file, serves with optional TLS from certs/,
and uses asfquart OAuth so redirect URIs work with localhost.apache.org.

  cp config.yaml.example config.yaml   # then edit secrets
  # generate PEMs under certs/ — see certs/README.md
  uv run python main.py
"""
from __future__ import annotations

import logging
import pathlib
import sys

_LOGGER = logging.getLogger(__name__)
DATE_FORMAT = "%m/%d %H:%M"

THIS_DIR = pathlib.Path(__file__).resolve().parent
CERTS_DIR = THIS_DIR / "certs"


def create_app():
    """Create the asfquart app with routes (also available as asfquart.APP)."""
    # Ensure the package is importable when launched as a script.
    if str(THIS_DIR) not in sys.path:
        sys.path.insert(0, str(THIS_DIR))

    from llmao.app import create_app as _create

    return _create(app_dir=str(THIS_DIR))


def run_standalone() -> None:
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
    port = int(getattr(server, "port", None) or (server.get("port") if hasattr(server, "get") else None) or 8443)

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


if __name__ == "__main__":
    run_standalone()

"""The portal sits behind one reverse proxy, and must not be told otherwise.

Hypercorn's ProxyFixMiddleware is configured here rather than written here,
so what is worth pinning is the choice of trusted_hops: getting it wrong is
silent in both directions. Too many and a caller picks the address we
record; too few and every request looks like it came from the proxy.

main.py is read rather than imported: importing it runs run_asgi(), which
builds the app and needs a real config.yaml.
"""

import asyncio
import pathlib
import re

from hypercorn.middleware import ProxyFixMiddleware

_MAIN = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text()
TRUSTED_HOPS = int(re.search(r"^TRUSTED_HOPS = (\d+)$", _MAIN, re.M).group(1))


def _client_seen(xff: str | None) -> str | None:
    """The address the app is handed, for a request carrying this header."""
    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"][0] if scope.get("client") else None

    wrapped = ProxyFixMiddleware(app, trusted_hops=TRUSTED_HOPS)
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    asyncio.run(wrapped({"type": "http", "headers": headers, "client": ("127.0.0.1", 0)}, None, None))
    return seen["client"]


def test_the_middleware_is_wired_into_the_asgi_entry_point():
    assert "ProxyFixMiddleware(create_app(), trusted_hops=TRUSTED_HOPS)" in _MAIN


def test_the_proxy_hop_is_replaced_by_the_caller():
    assert _client_seen("203.0.113.7") == "203.0.113.7"


def test_a_caller_cannot_choose_its_own_address():
    """Apache appends the real peer, so a forged entry lands to the left."""
    assert _client_seen("198.51.100.1, 203.0.113.7") == "203.0.113.7"


def test_without_the_header_the_peer_stands():
    assert _client_seen(None) == "127.0.0.1"

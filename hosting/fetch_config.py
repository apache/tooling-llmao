"""Fetch servers.yaml from asfquart. Fail fast on missing env or HTTP errors."""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_PATH = "/vllm/config/{set_id}"


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def config_url(asfquart_url: str, set_id: str) -> str:
    origin = asfquart_url.rstrip("/")
    return origin + CONFIG_PATH.format(set_id=set_id)


def fetch_yaml(url: str, fleet_key: str, timeout_s: float = 30.0) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {fleet_key}", "Accept": "application/yaml"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise SystemExit(f"config fetch HTTP {status} from {url}")
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"config fetch HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"config fetch failed: {exc.reason}") from exc
    if not body.strip():
        raise SystemExit("config fetch returned empty body")
    return body


def write_yaml(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def main(argv: list[str] | None = None) -> int:
    del argv  # no flags; env is the interface
    fleet_key = require_env("FLEET_KEY")
    set_id = require_env("VLLM_SET")
    asfquart_url = require_env("ASFQUART_URL")
    dest = Path(os.environ.get("SERVERS_YAML", "/workspace/servers.yaml"))
    url = config_url(asfquart_url, set_id)
    write_yaml(dest, fetch_yaml(url, fleet_key))
    print(f"wrote {dest} from {url}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

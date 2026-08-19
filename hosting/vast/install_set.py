"""Vast box-start: fetch set JSON, write Supervisor units, exit.

Not a process manager. supervisord owns vllm serve. Other providers GET
the same /vllm/config/<set> JSON and emit their own artifacts.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONFIG_PATH = "/vllm/config/{set_id}"
CONF_DIR = Path(os.environ.get("SUPERVISOR_CONF_DIR", "/etc/supervisor/conf.d"))
VLLM_BIN = os.environ.get("VLLM_BIN", "/venv/main/bin/vllm")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def config_url(asfquart_url: str, set_id: str) -> str:
    return asfquart_url.rstrip("/") + CONFIG_PATH.format(set_id=set_id)


def ssl_context() -> ssl.SSLContext:
    # SSL_VERIFY=0: stopgap while llm.apache.org is :8443 (self-signed).
    # Drop when that host serves :443 with a public CA.
    raw = os.environ.get("SSL_VERIFY", "1").strip().lower()
    if raw in ("0", "false", "no"):
        print(
            "SSL_VERIFY=0: skipping TLS verify (remove when llm.apache.org is on :443)",
            file=sys.stderr,
        )
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def fetch_config(url: str, fleet_key: str, timeout_s: float = 30.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {fleet_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s, context=ssl_context()) as resp:
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
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"config fetch is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("config JSON must be an object")
    if "error" in data and "servers" not in data:
        raise SystemExit(f"config fetch error: {data['error']}")
    return data


def normalize_args(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, list):
        return [str(item) for item in raw]
    raise SystemExit(f"args must be a list or string, got {type(raw).__name__}")


def parse_server(raw: dict[str, Any]) -> dict[str, Any]:
    missing = [k for k in ("name", "model", "port", "api_key") if not raw.get(k)]
    if missing:
        raise SystemExit(f"server entry missing required fields: {', '.join(missing)}")
    gmu = raw.get("gpu_memory_utilization")
    mml = raw.get("max_model_len")
    return {
        "name": str(raw["name"]),
        "model": str(raw["model"]),
        "port": int(raw["port"]),
        "api_key": str(raw["api_key"]),
        "gpu_memory_utilization": float(gmu) if gmu is not None else None,
        "max_model_len": int(mml) if mml is not None else None,
        "args": normalize_args(raw.get("args")),
    }


def servers_from_config(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("servers")
    if not rows:
        raise SystemExit("config JSON has no servers")
    return [parse_server(s) for s in rows]


def build_argv(spec: dict[str, Any]) -> list[str]:
    cmd = [
        VLLM_BIN,
        "serve",
        spec["model"],
        "--host",
        "0.0.0.0",
        "--port",
        str(spec["port"]),
    ]
    if spec.get("gpu_memory_utilization") is not None:
        cmd.extend(["--gpu-memory-utilization", str(spec["gpu_memory_utilization"])])
    if spec.get("max_model_len") is not None:
        cmd.extend(["--max-model-len", str(spec["max_model_len"])])
    cmd.extend(spec["args"])
    return cmd


def program_name(spec_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", spec_name).strip("-") or "model"
    return f"vllm-{slug}"


def _ini_escape(value: str) -> str:
    return value.replace("%", "%%")


def program_ini(spec: dict[str, Any], *, hf_home: str, log_dir: str) -> str:
    name = program_name(spec["name"])
    command = shlex.join(build_argv(spec))
    log = str(Path(log_dir) / f"{spec['name']}.log")
    env = (
        f'HF_HOME="{_ini_escape(hf_home)}",'
        f'VLLM_API_KEY="{_ini_escape(spec["api_key"])}"'
    )
    return (
        f"[program:{name}]\n"
        f"command={_ini_escape(command)}\n"
        f"directory=/workspace\n"
        f"autostart=true\n"
        f"autorestart=true\n"
        f"startretries=5\n"
        f"environment={env}\n"
        f"stdout_logfile={_ini_escape(log)}\n"
        f"stderr_logfile={_ini_escape(log)}\n"
    )


def write_units(data: dict[str, Any], conf_dir: Path) -> list[Path]:
    hf_home = str(data.get("hf_home") or "/workspace/hf-cache")
    log_dir = str(data.get("log_dir") or "/workspace/logs")
    Path(hf_home).mkdir(parents=True, exist_ok=True)
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    conf_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for spec in servers_from_config(data):
        path = conf_dir / f"{program_name(spec['name'])}.conf"
        path.write_text(program_ini(spec, hf_home=hf_home, log_dir=log_dir))
        path.chmod(0o600)
        written.append(path)
        print(f"wrote {path}", file=sys.stderr)
    return written


def supervisorctl_update() -> None:
    subprocess.check_call(["supervisorctl", "reread"])
    subprocess.check_call(["supervisorctl", "update"])


def main(argv: list[str] | None = None) -> int:
    del argv
    fleet_key = require_env("FLEET_KEY")
    set_id = require_env("VLLM_SET")
    asfquart_url = require_env("ASFQUART_URL")
    url = config_url(asfquart_url, set_id)
    data = fetch_config(url, fleet_key)
    write_units(data, CONF_DIR)
    if os.environ.get("INSTALL_SET_DRY_RUN", "").strip() in ("1", "true"):
        return 0
    supervisorctl_update()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

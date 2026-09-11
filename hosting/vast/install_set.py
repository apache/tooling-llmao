"""Vast box-start: fetch set JSON, write Supervisor units, exit.

Not a process manager. supervisord owns vllm serve. Other providers GET
the same /vllm/config JSON and emit their own artifacts.
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

CONFIG_PATH = "/vllm/config"
CONF_DIR = Path(os.environ.get("SUPERVISOR_CONF_DIR", "/etc/supervisor/conf.d"))
VLLM_BIN = os.environ.get("VLLM_BIN", "/usr/local/bin/vllm")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def config_url(asfquart_url: str) -> str:
    return asfquart_url.rstrip("/") + CONFIG_PATH


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
    vram = raw.get("vram_gb")
    disk = raw.get("disk_gb")
    return {
        "name": str(raw["name"]),
        "model": str(raw["model"]),
        "port": int(raw["port"]),
        "api_key": str(raw["api_key"]),
        "gpu_memory_utilization": float(gmu) if gmu is not None else None,
        "max_model_len": int(mml) if mml is not None else None,
        "vram_gb": float(vram) if vram is not None else None,
        "disk_gb": float(disk) if disk is not None else None,
        "args": normalize_args(raw.get("args")),
    }


def servers_from_config(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("servers")
    if not rows:
        raise SystemExit("config JSON has no servers")
    return [parse_server(s) for s in rows]


def free_vram_gb() -> float | None:
    """Free VRAM on GPU 0, or None if nvidia-smi is unavailable.

    Capacity is read from the card rather than declared in the catalog: a
    hand-typed figure is wrong the first time a provider supplies a different
    GPU than was ordered, and with rented instances that is a matter of when.
    """
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    try:
        return int(out[0].strip()) / 1024.0
    except ValueError:
        return None


def free_disk_gb(path: str) -> float | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return (st.f_bavail * st.f_frsize) / (1024.0 ** 3)


def check_fit(specs: list[dict[str, Any]], *, data_dir: str) -> list[str]:
    """Reasons these servers will not fit, or an empty list.

    Checked before writing units because the alternative is a fifteen-minute
    weights pull followed by an engine-init failure whose message does not
    mention memory. Requirements come from the catalog; capacity is read from
    the box.

    Silent when nvidia-smi is unavailable or a model declares no vram_gb --
    an unknown is not a failure, and refusing to start on a missing optional
    field would be worse than the problem.
    """
    problems: list[str] = []

    want_vram = sum(s["vram_gb"] for s in specs if s.get("vram_gb"))
    have_vram = free_vram_gb()
    if want_vram and have_vram is not None and want_vram > have_vram:
        names = ", ".join(s["name"] for s in specs if s.get("vram_gb"))
        problems.append(
            f"VRAM: {names} need {want_vram:.1f}GB, {have_vram:.1f}GB free. "
            f"Weights alone -- KV cache comes out of what is left."
        )

    want_disk = sum(s["disk_gb"] for s in specs if s.get("disk_gb"))
    have_disk = free_disk_gb(data_dir)
    if want_disk and have_disk is not None and want_disk > have_disk:
        problems.append(
            f"disk: need {want_disk:.1f}GB under {data_dir}, {have_disk:.1f}GB free"
        )

    return problems


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


def program_ini(spec: dict[str, Any], *, hf_home: str, log_dir: str, data_dir: str) -> str:
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
        f"directory={_ini_escape(data_dir)}\n"
        f"autostart=true\n"
        f"autorestart=true\n"
        f"startretries=5\n"
        f"environment={env}\n"
        f"stdout_logfile={_ini_escape(log)}\n"
        f"stderr_logfile={_ini_escape(log)}\n"
    )


def write_units(data: dict[str, Any], conf_dir: Path) -> list[Path]:
    data_dir = Path(require_env("DATA_DIRECTORY"))
    hf_home = data_dir / "hf-cache"
    log_dir = data_dir / "logs"
    hf_home.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    conf_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for spec in servers_from_config(data):
        path = conf_dir / f"{program_name(spec['name'])}.conf"
        path.write_text(
            program_ini(
                spec,
                hf_home=str(hf_home),
                log_dir=str(log_dir),
                data_dir=str(data_dir),
            )
        )
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
    asfquart_url = require_env("ASFQUART_URL")
    url = config_url(asfquart_url)
    data = fetch_config(url, fleet_key)

    # Refuse before writing units rather than after a long weights pull. The
    # provisioning log is where this is read, so the message has to stand on
    # its own -- there is no reporting channel back to llmao yet.
    data_dir = os.environ.get("DATA_DIRECTORY", "/workspace")
    problems = check_fit(servers_from_config(data), data_dir=data_dir)
    if problems:
        for line in problems:
            print(f"install_set: will not fit -- {line}", file=sys.stderr)
        return 1

    write_units(data, CONF_DIR)
    if os.environ.get("INSTALL_SET_DRY_RUN", "").strip() in ("1", "true"):
        return 0
    supervisorctl_update()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

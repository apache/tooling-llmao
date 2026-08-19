"""Fetch set JSON from asfquart and launch vLLM. No GPU required to import/test."""

from __future__ import annotations

import json
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_PATH = "/vllm/config/{set_id}"


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def config_url(asfquart_url: str, set_id: str) -> str:
    return asfquart_url.rstrip("/") + CONFIG_PATH.format(set_id=set_id)


def ssl_context() -> ssl.SSLContext:
    # SSL_VERIFY=0 is a stopgap while llm.apache.org is on :8443 with a
    # self-signed/mkcert cert. Drop it when that host serves :443 with a
    # public CA (Let's Encrypt / ASF).
    raw = os.environ.get("SSL_VERIFY", "1").strip().lower()
    if raw in ("0", "false", "no"):
        ctx = ssl._create_unverified_context()
        print(
            "SSL_VERIFY=0: skipping TLS verify (remove when llm.apache.org is on :443)",
            file=sys.stderr,
        )
        return ctx
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


@dataclass(frozen=True)
class ServerSpec:
    name: str
    model: str
    port: int
    api_key: str
    gpu_memory_utilization: float | None = None
    max_model_len: int | None = None
    args: tuple[str, ...] = ()


@dataclass
class FleetConfig:
    hf_home: str
    log_dir: str
    servers: list[ServerSpec]


def normalize_args(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(raw.split())
    if isinstance(raw, list):
        return tuple(str(item) for item in raw)
    raise SystemExit(f"args must be a list or string, got {type(raw).__name__}")


def parse_server(raw: dict[str, Any]) -> ServerSpec:
    missing = [k for k in ("name", "model", "port", "api_key") if not raw.get(k)]
    if missing:
        raise SystemExit(f"server entry missing required fields: {', '.join(missing)}")
    gmu = raw.get("gpu_memory_utilization")
    mml = raw.get("max_model_len")
    return ServerSpec(
        name=str(raw["name"]),
        model=str(raw["model"]),
        port=int(raw["port"]),
        api_key=str(raw["api_key"]),
        gpu_memory_utilization=float(gmu) if gmu is not None else None,
        max_model_len=int(mml) if mml is not None else None,
        args=normalize_args(raw.get("args")),
    )


def load_config(data: dict[str, Any]) -> FleetConfig:
    servers = data.get("servers")
    if not servers:
        raise SystemExit("config JSON has no servers")
    return FleetConfig(
        hf_home=str(data.get("hf_home") or "/workspace/hf-cache"),
        log_dir=str(data.get("log_dir") or "/workspace/logs"),
        servers=[parse_server(s) for s in servers],
    )


def build_argv(spec: ServerSpec) -> list[str]:
    cmd = [
        "vllm",
        "serve",
        spec.model,
        "--host",
        "0.0.0.0",
        "--port",
        str(spec.port),
        "--api-key",
        spec.api_key,
    ]
    if spec.gpu_memory_utilization is not None:
        cmd.extend(["--gpu-memory-utilization", str(spec.gpu_memory_utilization)])
    if spec.max_model_len is not None:
        cmd.extend(["--max-model-len", str(spec.max_model_len)])
    cmd.extend(spec.args)
    return cmd


@dataclass
class Child:
    spec: ServerSpec
    proc: subprocess.Popen | None = None
    restarts: int = 0
    log_fh: Any = None


@dataclass
class Supervisor:
    cfg: FleetConfig
    max_restarts: int
    children: list[Child] = field(default_factory=list)
    _stop: bool = False

    def start_all(self) -> None:
        Path(self.cfg.hf_home).mkdir(parents=True, exist_ok=True)
        Path(self.cfg.log_dir).mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = self.cfg.hf_home
        for spec in self.cfg.servers:
            child = Child(spec=spec)
            self._spawn(child)
            self.children.append(child)

    def _spawn(self, child: Child) -> None:
        log_path = Path(self.cfg.log_dir) / f"{child.spec.name}.log"
        if child.log_fh is not None:
            child.log_fh.close()
        child.log_fh = open(log_path, "ab")
        argv = build_argv(child.spec)
        print(f"starting {child.spec.name}: {' '.join(argv)}", file=sys.stderr)
        child.proc = subprocess.Popen(
            argv,
            stdout=child.log_fh,
            stderr=subprocess.STDOUT,
            env=os.environ.copy(),
        )

    def request_stop(self, *_args: Any) -> None:
        self._stop = True

    def run(self, poll_s: float = 2.0) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        self.start_all()
        while not self._stop:
            time.sleep(poll_s)
            for child in self.children:
                if child.proc is None:
                    continue
                rc = child.proc.poll()
                if rc is None:
                    continue
                print(
                    f"{child.spec.name} exited {rc} (restarts={child.restarts})",
                    file=sys.stderr,
                )
                if child.restarts >= self.max_restarts:
                    print(
                        f"{child.spec.name}: giving up after {self.max_restarts} restarts",
                        file=sys.stderr,
                    )
                    child.proc = None
                    continue
                child.restarts += 1
                backoff = min(30, 2 ** child.restarts)
                time.sleep(backoff)
                if self._stop:
                    break
                self._spawn(child)
            if all(c.proc is None for c in self.children):
                print("all servers given up; supervisor idle", file=sys.stderr)
                while not self._stop:
                    time.sleep(poll_s)
        self._terminate_all()
        return 0

    def _terminate_all(self) -> None:
        for child in self.children:
            if child.proc is None or child.proc.poll() is not None:
                continue
            child.proc.terminate()
        deadline = time.time() + 15
        for child in self.children:
            if child.proc is None:
                continue
            remaining = max(0.1, deadline - time.time())
            try:
                child.proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                child.proc.kill()
            if child.log_fh is not None:
                child.log_fh.close()
                child.log_fh = None


def main(argv: list[str] | None = None) -> int:
    del argv
    fleet_key = require_env("FLEET_KEY")
    set_id = require_env("VLLM_SET")
    asfquart_url = require_env("ASFQUART_URL")
    max_restarts = int(os.environ.get("LAUNCHER_MAX_RESTARTS", "5"))
    url = config_url(asfquart_url, set_id)
    cfg = load_config(fetch_config(url, fleet_key))
    return Supervisor(cfg, max_restarts=max_restarts).run()


if __name__ == "__main__":
    raise SystemExit(main())

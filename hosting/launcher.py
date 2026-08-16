"""Launch vLLM processes from servers.yaml. No GPU required to import/test."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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


def load_config(path: Path) -> FleetConfig:
    if not path.is_file():
        raise SystemExit(f"servers.yaml not found: {path}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise SystemExit("servers.yaml must be a mapping")
    servers = data.get("servers")
    if not servers:
        raise SystemExit("servers.yaml has no servers")
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
                # Stay up so SSH still works; wait for signal.
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
    path = Path(os.environ.get("SERVERS_YAML", "/workspace/servers.yaml"))
    max_restarts = int(os.environ.get("LAUNCHER_MAX_RESTARTS", "5"))
    cfg = load_config(path)
    return Supervisor(cfg, max_restarts=max_restarts).run()


if __name__ == "__main__":
    raise SystemExit(main())

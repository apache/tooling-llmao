"""GPU-free tests for hosting/ fetch + launcher (no vLLM process)."""
import http.server
import os
import sys
import threading
from pathlib import Path

import pytest
import yaml

HOSTING = Path(__file__).resolve().parents[1] / "hosting"
sys.path.insert(0, str(HOSTING))

import fetch_config  # noqa: E402
import launcher  # noqa: E402

SAMPLE = """
hf_home: /tmp/hf
log_dir: /tmp/logs
servers:
  - name: model-a
    model: org/model-a
    port: 8000
    api_key: sk-aaa
    gpu_memory_utilization: 0.42
    max_model_len: 16384
    args:
      - --dtype
      - auto
  - name: model-b
    model: org/model-b
    port: 8001
    api_key: sk-bbb
    args: "--enable-prefix-caching"
"""


def test_parse_and_argv(tmp_path):
    path = tmp_path / "servers.yaml"
    path.write_text(SAMPLE)
    cfg = launcher.load_config(path)
    assert cfg.hf_home == "/tmp/hf"
    assert len(cfg.servers) == 2
    a = cfg.servers[0]
    assert a.name == "model-a"
    assert launcher.build_argv(a) == [
        "vllm",
        "serve",
        "org/model-a",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--api-key",
        "sk-aaa",
        "--gpu-memory-utilization",
        "0.42",
        "--max-model-len",
        "16384",
        "--dtype",
        "auto",
    ]
    b = cfg.servers[1]
    assert "--enable-prefix-caching" in launcher.build_argv(b)
    assert "--gpu-memory-utilization" not in launcher.build_argv(b)


def test_missing_required_field(tmp_path):
    path = tmp_path / "servers.yaml"
    path.write_text(yaml.dump({"servers": [{"name": "x", "model": "m", "port": 1}]}))
    with pytest.raises(SystemExit, match="api_key"):
        launcher.load_config(path)


def test_missing_yaml(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        launcher.load_config(tmp_path / "nope.yaml")


def test_fetch_writes_file(tmp_path, monkeypatch):
    body = SAMPLE.encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer fleet-secret":
                self.send_error(403)
                return
            if self.path != "/vllm/config/coding":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/yaml")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    dest = tmp_path / "servers.yaml"
    monkeypatch.setenv("FLEET_KEY", "fleet-secret")
    monkeypatch.setenv("VLLM_SET", "coding")
    monkeypatch.setenv("ASFQUART_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("SERVERS_YAML", str(dest))
    assert fetch_config.main() == 0
    httpd.shutdown()
    loaded = yaml.safe_load(dest.read_text())
    assert loaded["servers"][0]["name"] == "model-a"


def test_fetch_missing_env(monkeypatch):
    for key in ("FLEET_KEY", "VLLM_SET", "ASFQUART_URL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit, match="FLEET_KEY"):
        fetch_config.main()


def test_config_url_strips_slash():
    assert fetch_config.config_url("https://x.example/", "box-a") == (
        "https://x.example/vllm/config/box-a"
    )

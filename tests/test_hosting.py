"""GPU-free tests for hosting/launcher (JSON fetch, no vLLM process)."""
import http.server
import json
import ssl
import sys
from pathlib import Path

import pytest

HOSTING = Path(__file__).resolve().parents[1] / "hosting"
sys.path.insert(0, str(HOSTING))

import launcher  # noqa: E402

SAMPLE = {
    "set_id": "primary",
    "hf_home": "/tmp/hf",
    "log_dir": "/tmp/logs",
    "servers": [
        {
            "name": "model-a",
            "model": "org/model-a",
            "port": 8000,
            "api_key": "sk-aaa",
            "gpu_memory_utilization": 0.42,
            "max_model_len": 16384,
            "args": ["--dtype", "auto"],
        },
        {
            "name": "model-b",
            "model": "org/model-b",
            "port": 8001,
            "api_key": "sk-bbb",
            "args": "--enable-prefix-caching",
        },
    ],
}


def test_parse_and_argv():
    cfg = launcher.load_config(SAMPLE)
    assert cfg.hf_home == "/tmp/hf"
    assert len(cfg.servers) == 2
    a = cfg.servers[0]
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


def test_missing_required_field():
    with pytest.raises(SystemExit, match="api_key"):
        launcher.load_config({"servers": [{"name": "x", "model": "m", "port": 1}]})


def test_empty_servers():
    with pytest.raises(SystemExit, match="no servers"):
        launcher.load_config({"servers": []})


def test_fetch_json(monkeypatch):
    body = json.dumps(SAMPLE).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer fleet-secret":
                self.send_error(403)
                return
            if self.path != "/vllm/config/primary":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    import threading

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("SSL_VERIFY", "1")
    url = f"http://127.0.0.1:{port}/vllm/config/primary"
    data = launcher.fetch_config(url, "fleet-secret")
    httpd.shutdown()
    assert data["servers"][0]["name"] == "model-a"


def test_fetch_missing_env(monkeypatch):
    for key in ("FLEET_KEY", "VLLM_SET", "ASFQUART_URL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SystemExit, match="FLEET_KEY"):
        launcher.require_env("FLEET_KEY")


def test_config_url_strips_slash():
    assert launcher.config_url("https://x.example/", "box-a") == (
        "https://x.example/vllm/config/box-a"
    )


def test_ssl_verify_off(monkeypatch):
    monkeypatch.setenv("SSL_VERIFY", "0")
    ctx = launcher.ssl_context()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE

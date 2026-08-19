"""GPU-free tests for hosting/vast/install_set.py (no vLLM, no supervisord)."""
import http.server
import json
import ssl
import sys
import threading
from pathlib import Path

import pytest

VAST = Path(__file__).resolve().parents[1] / "hosting" / "vast"
sys.path.insert(0, str(VAST))

import install_set as inst  # noqa: E402

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


def test_build_argv(monkeypatch):
    monkeypatch.setenv("VLLM_BIN", "vllm")
    # re-import uses module-level VLLM_BIN captured at load; call with patched attr
    monkeypatch.setattr(inst, "VLLM_BIN", "vllm")
    spec = inst.parse_server(SAMPLE["servers"][0])
    assert inst.build_argv(spec) == [
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
    b = inst.parse_server(SAMPLE["servers"][1])
    argv = inst.build_argv(b)
    assert "--enable-prefix-caching" in argv
    assert "--gpu-memory-utilization" not in argv


def test_missing_required_field():
    with pytest.raises(SystemExit, match="api_key"):
        inst.parse_server({"name": "x", "model": "m", "port": 1})


def test_empty_servers():
    with pytest.raises(SystemExit, match="no servers"):
        inst.servers_from_config({"servers": []})


def test_program_ini_and_write(tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "VLLM_BIN", "vllm")
    data = {
        **SAMPLE,
        "hf_home": str(tmp_path / "hf"),
        "log_dir": str(tmp_path / "logs"),
    }
    written = inst.write_units(data, tmp_path / "conf.d")
    assert len(written) == 2
    text = written[0].read_text()
    assert "[program:vllm-model-a]" in text
    assert "vllm serve org/model-a" in text
    assert "autorestart=true" in text
    assert f'HF_HOME="{tmp_path / "hf"}"' in text


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
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setenv("SSL_VERIFY", "1")
    url = f"http://127.0.0.1:{port}/vllm/config/primary"
    data = inst.fetch_config(url, "fleet-secret")
    httpd.shutdown()
    assert data["servers"][0]["name"] == "model-a"


def test_config_url_strips_slash():
    assert inst.config_url("https://x.example/", "box-a") == (
        "https://x.example/vllm/config/box-a"
    )


def test_ssl_verify_off(monkeypatch):
    monkeypatch.setenv("SSL_VERIFY", "0")
    ctx = inst.ssl_context()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE

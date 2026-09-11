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
    "host": "127.0.0.1",
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
    monkeypatch.setenv("DATA_DIRECTORY", str(tmp_path))
    written = inst.write_units(SAMPLE, tmp_path / "conf.d")
    assert len(written) == 2
    text = written[0].read_text()
    assert "[program:vllm-model-a]" in text
    assert "vllm serve org/model-a" in text
    assert "--api-key" not in text
    assert "sk-aaa" not in text.split("environment=", 1)[0]
    assert 'VLLM_API_KEY="sk-aaa"' in text
    assert "autorestart=true" in text
    assert f'HF_HOME="{tmp_path / "hf-cache"}"' in text
    assert f"directory={tmp_path}" in text
    assert written[0].stat().st_mode & 0o777 == 0o600


def test_fetch_json(monkeypatch):
    body = json.dumps(SAMPLE).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer fleet-secret":
                self.send_error(403)
                return
            if self.path != "/vllm/config":
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
    url = f"http://127.0.0.1:{port}/vllm/config"
    data = inst.fetch_config(url, "fleet-secret")
    httpd.shutdown()
    assert data["servers"][0]["name"] == "model-a"


def test_config_url_strips_slash():
    assert inst.config_url("https://x.example/") == "https://x.example/vllm/config"


def test_ssl_verify_off(monkeypatch):
    monkeypatch.setenv("SSL_VERIFY", "0")
    ctx = inst.ssl_context()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


def _spec(name="gemma4-26b", vram=None, disk=None):
    raw = {"name": name, "model": "org/model", "port": 8001, "api_key": "sk-x"}
    if vram is not None:
        raw["vram_gb"] = vram
    if disk is not None:
        raw["disk_gb"] = disk
    return inst.parse_server(raw)


def test_parse_server_reads_requirements():
    spec = _spec(vram=49, disk=55)
    assert spec["vram_gb"] == 49.0
    assert spec["disk_gb"] == 55.0


def test_parse_server_requirements_optional():
    spec = _spec()
    assert spec["vram_gb"] is None
    assert spec["disk_gb"] is None


def test_check_fit_passes_when_it_fits(monkeypatch):
    monkeypatch.setattr(inst, "free_vram_gb", lambda: 80.0)
    monkeypatch.setattr(inst, "free_disk_gb", lambda p: 100.0)
    assert inst.check_fit([_spec(vram=49, disk=55)], data_dir="/workspace") == []


def test_check_fit_reports_vram_shortfall(monkeypatch):
    """The case this exists for: a 49GB model on a 24GB card.

    Without the check, the box pulls ~50GB of weights and then fails at engine
    init with a message that does not mention memory.
    """
    monkeypatch.setattr(inst, "free_vram_gb", lambda: 24.0)
    monkeypatch.setattr(inst, "free_disk_gb", lambda p: 500.0)
    problems = inst.check_fit([_spec(vram=49)], data_dir="/workspace")
    assert len(problems) == 1
    assert "VRAM" in problems[0]
    assert "49.0" in problems[0] and "24.0" in problems[0]


def test_check_fit_sums_across_co_resident_servers(monkeypatch):
    """Two models on one card compete for the same VRAM.

    Gemma and Qwen were briefly co-resident on an L40S; together they left too
    little for KV cache, forcing Qwen to 8k context and costing Gemma its CUDA
    graphs. Neither fails on its own.
    """
    monkeypatch.setattr(inst, "free_vram_gb", lambda: 48.0)
    monkeypatch.setattr(inst, "free_disk_gb", lambda p: 500.0)
    specs = [_spec("gemma4-26b", vram=49), _spec("qwen3-8b", vram=8)]
    problems = inst.check_fit(specs, data_dir="/workspace")
    assert len(problems) == 1
    assert "57.0" in problems[0]


def test_check_fit_silent_without_nvidia_smi(monkeypatch):
    """An unknown is not a failure.

    Refusing to start because nvidia-smi is missing would be worse than the
    problem the check exists to catch.
    """
    monkeypatch.setattr(inst, "free_vram_gb", lambda: None)
    monkeypatch.setattr(inst, "free_disk_gb", lambda p: None)
    assert inst.check_fit([_spec(vram=49, disk=55)], data_dir="/workspace") == []


def test_check_fit_silent_when_model_declares_nothing(monkeypatch):
    monkeypatch.setattr(inst, "free_vram_gb", lambda: 4.0)
    monkeypatch.setattr(inst, "free_disk_gb", lambda p: 4.0)
    assert inst.check_fit([_spec()], data_dir="/workspace") == []


def test_check_fit_reports_disk_shortfall(monkeypatch):
    monkeypatch.setattr(inst, "free_vram_gb", lambda: 80.0)
    monkeypatch.setattr(inst, "free_disk_gb", lambda p: 20.0)
    problems = inst.check_fit([_spec(disk=55)], data_dir="/workspace")
    assert len(problems) == 1
    assert "disk" in problems[0]

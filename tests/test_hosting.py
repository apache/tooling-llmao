# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""GPU-free tests for hosting/install_set.py (no vLLM, no supervisord)."""

import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

HOSTING = Path(__file__).resolve().parents[1] / "hosting"
sys.path.insert(0, str(HOSTING))

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


def test_vllm_answers_to_the_name_litellm_asks_for(monkeypatch):
    monkeypatch.setattr(inst, "VLLM_BIN", "vllm")
    spec = inst.parse_server({**SAMPLE["servers"][1], "served_model_name": "qwen3-8b-bf16"})
    argv = inst.build_argv(spec)
    assert argv[argv.index("--served-model-name") + 1] == "qwen3-8b-bf16"
    # From a control plane older than the field: vLLM's default, as before.
    assert "--served-model-name" not in inst.build_argv(inst.parse_server(SAMPLE["servers"][1]))


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
    written = inst.write_units(inst.servers_from_config(SAMPLE), tmp_path / "conf.d")
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


def test_fetch_json():
    body = json.dumps(SAMPLE).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Authorization") != "Bearer fleet-secret":
                self.send_error(403)
                return
            if self.path != "/vllm/config":
                self.send_error(404)
                return
            if self.headers.get("X-LLMAO-Host"):
                self.send_error(400)
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
    url = f"http://127.0.0.1:{port}/vllm/config"
    data = inst.fetch_config(url, "fleet-secret")
    httpd.shutdown()
    assert data["servers"][0]["name"] == "model-a"


def test_fetch_sends_host_header():
    body = json.dumps(SAMPLE).encode()
    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["host"] = self.headers.get("X-LLMAO-Host")
            if self.headers.get("Authorization") != "Bearer fleet-secret":
                self.send_error(403)
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
    url = f"http://127.0.0.1:{port}/vllm/config"
    data = inst.fetch_config(url, "fleet-secret", host="98.142.241.142")
    httpd.shutdown()
    assert seen["host"] == "98.142.241.142"
    assert data["servers"][0]["name"] == "model-a"


def test_config_url_strips_slash():
    assert inst.config_url("https://x.example/") == "https://x.example/vllm/config"


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


def test_check_fit_counts_disk_once_per_model(monkeypatch):
    """Two servers of one model share a single copy of the weights."""
    monkeypatch.setattr(inst, "free_vram_gb", lambda: None)
    monkeypatch.setattr(inst, "free_disk_gb", lambda p: 60.0)
    same = [_spec(name="a", disk=55), _spec(name="b", disk=55)]
    assert inst.check_fit(same, data_dir="/workspace") == []

    other = _spec(name="c", disk=55)
    other["model"] = "another/model"
    problems = inst.check_fit([same[0], other], data_dir="/workspace")
    assert len(problems) == 1
    assert "need 110.0GB" in problems[0]


def test_check_fit_reports_disk_shortfall(monkeypatch):
    monkeypatch.setattr(inst, "free_vram_gb", lambda: 80.0)
    monkeypatch.setattr(inst, "free_disk_gb", lambda p: 20.0)
    problems = inst.check_fit([_spec(disk=55)], data_dir="/workspace")
    assert len(problems) == 1
    assert "disk" in problems[0]


def _sized(name, vram=None, mml=None, gmu=None):
    raw = {"name": name, "model": f"org/{name}", "port": 8000, "api_key": "sk-x"}
    if vram is not None:
        raw["vram_gb"] = vram
    if mml is not None:
        raw["max_model_len"] = mml
    if gmu is not None:
        raw["gpu_memory_utilization"] = gmu
    return inst.parse_server(raw)


def test_apportion_two_identical_servers_share_the_card():
    """The case that prompted this: two qwen3 on one card."""
    specs = inst.apportion([_sized("a", vram=9, mml=40960), _sized("b", vram=9, mml=40960)], 48.0)
    assert [s["gpu_memory_utilization"] for s in specs] == [0.45, 0.45]


def test_apportion_splits_the_leftover_by_context_window():
    big = _sized("gemma", vram=49, mml=131072)
    small = _sized("qwen", vram=9, mml=40960)
    inst.apportion([big, small], 80.0)
    kv_big = big["gpu_memory_utilization"] * 80 - 49
    kv_small = small["gpu_memory_utilization"] * 80 - 9
    assert kv_big / kv_small == pytest.approx(131072 / 40960, rel=0.05)
    assert big["gpu_memory_utilization"] + small["gpu_memory_utilization"] <= inst.GPU_USABLE_FRACTION


def test_apportion_splits_equally_without_a_context_window():
    a = _sized("a", vram=10, mml=131072)
    b = _sized("b", vram=10)
    inst.apportion([a, b], 100.0)
    assert a["gpu_memory_utilization"] == b["gpu_memory_utilization"] == 0.45


def test_apportion_lone_server_gets_the_vllm_default():
    spec = _sized("a", vram=9, mml=40960)
    inst.apportion([spec], 48.0)
    assert spec["gpu_memory_utilization"] == pytest.approx(0.9, abs=0.001)


def test_apportion_keeps_and_deducts_an_explicit_fraction():
    pinned = _sized("a", vram=9, gmu=0.5)
    other = _sized("b", vram=9, mml=40960)
    inst.apportion([pinned, other], 48.0)
    assert pinned["gpu_memory_utilization"] == 0.5
    assert other["gpu_memory_utilization"] == pytest.approx(0.4, abs=0.001)


def test_apportion_leaves_unknowns_alone():
    # No nvidia-smi.
    spec = _sized("a", vram=9, mml=40960)
    assert inst.apportion([spec], None)[0]["gpu_memory_utilization"] is None
    # A server that declares no weights: nothing to reserve for it.
    specs = inst.apportion([_sized("a", vram=9), _sized("b")], 48.0)
    assert [s["gpu_memory_utilization"] for s in specs] == [None, None]
    # The weights alone do not fit. check_fit reports that.
    specs = inst.apportion([_sized("a", vram=30), _sized("b", vram=30)], 48.0)
    assert [s["gpu_memory_utilization"] for s in specs] == [None, None]


def test_units_for_two_servers_each_carry_their_share(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIRECTORY", str(tmp_path))
    a = _sized("qwen-a", vram=9, mml=40960)
    b = _sized("qwen-b", vram=9, mml=40960)
    b["port"] = 8001
    written = inst.write_units(inst.apportion([a, b], 48.0), tmp_path / "conf.d")
    assert len(written) == 2
    for path in written:
        assert "--gpu-memory-utilization 0.45" in path.read_text()


def _smi(monkeypatch, stdout=None, *, missing=False):
    def run(*_args, **_kwargs):
        if missing:
            raise FileNotFoundError("nvidia-smi")
        return inst.subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(inst.subprocess, "run", run)


def test_vram_comes_from_nvidia_smi_on_a_discrete_card(monkeypatch):
    _smi(monkeypatch, "49140\n")
    assert inst.total_vram_gb() == pytest.approx(49140 / 1024)


def test_unified_memory_falls_back_to_meminfo(monkeypatch, tmp_path):
    """GB10: nvidia-smi answers, but with "[N/A]" for memory."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       125444852 kB\nMemFree:         1000 kB\nMemAvailable:   104857600 kB\n")
    monkeypatch.setattr(inst, "MEMINFO", meminfo)
    _smi(monkeypatch, "[N/A]\n")
    assert inst.total_vram_gb() == pytest.approx(125444852 / 1024**2)
    assert inst.free_vram_gb() == pytest.approx(100.0)


def test_no_nvidia_smi_is_still_unknown(monkeypatch, tmp_path):
    """System RAM is not GPU memory on a box with no GPU tooling at all."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       125444852 kB\n")
    monkeypatch.setattr(inst, "MEMINFO", meminfo)
    _smi(monkeypatch, missing=True)
    assert inst.total_vram_gb() is None
    # And an unreadable meminfo after "[N/A]" is an unknown, not a crash.
    monkeypatch.setattr(inst, "MEMINFO", tmp_path / "absent")
    _smi(monkeypatch, "[N/A]\n")
    assert inst.total_vram_gb() is None


def test_apportion_budgets_from_free_memory_as_a_fraction_of_total():
    """Unified memory: 128GB in the machine, 100GB of it available."""
    a = _sized("a", vram=9, mml=40960)
    b = _sized("b", vram=9, mml=40960)
    inst.apportion([a, b], 128.0, 100.0)
    # 0.9 of the 100GB available is 90GB, 45GB each, which is 0.351 of 128GB.
    assert a["gpu_memory_utilization"] == b["gpu_memory_utilization"] == 0.351
    # A pinned fraction is a fraction of the total, and comes out of the budget.
    pinned = _sized("p", vram=9, gmu=0.25)
    other = _sized("o", vram=9, mml=40960)
    inst.apportion([pinned, other], 128.0, 100.0)
    assert other["gpu_memory_utilization"] == pytest.approx((90 - 32) / 128, abs=0.001)


def _config_server(seen: dict):
    """A /vllm/config that records the host the box claimed to be."""
    body = json.dumps(SAMPLE).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["host"] = self.headers.get("X-LLMAO-Host")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _box_env(monkeypatch, tmp_path, httpd):
    monkeypatch.setenv("FLEET_KEY", "fleet-secret")
    monkeypatch.setenv("ASFQUART_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("LLMAO_HOST", "198.51.100.8")
    monkeypatch.setenv("DATA_DIRECTORY", str(tmp_path))
    monkeypatch.setattr(inst, "CONF_DIR", tmp_path / "conf.d")
    _smi(monkeypatch, missing=True)
    reloads = []
    monkeypatch.setattr(inst, "supervisorctl_update", lambda: reloads.append(True))
    return reloads


def test_main_claims_llmao_host_and_reloads_supervisord(monkeypatch, tmp_path):
    seen = {}
    httpd = _config_server(seen)
    reloads = _box_env(monkeypatch, tmp_path, httpd)
    try:
        assert inst.main([]) == 0
    finally:
        httpd.shutdown()
    assert seen["host"] == "198.51.100.8"
    assert sorted(p.name for p in (tmp_path / "conf.d").iterdir()) == ["vllm-model-a.conf", "vllm-model-b.conf"]
    assert reloads == [True]


def test_main_no_reload_writes_units_and_leaves_supervisord_alone(monkeypatch, tmp_path):
    """Where supervisord is not running yet, it reads the units when it starts."""
    httpd = _config_server({})
    reloads = _box_env(monkeypatch, tmp_path, httpd)
    try:
        assert inst.main(["--no-reload"]) == 0
    finally:
        httpd.shutdown()
    assert len(list((tmp_path / "conf.d").iterdir())) == 2
    assert reloads == []


def test_main_requires_llmao_host(monkeypatch, tmp_path):
    httpd = _config_server({})
    _box_env(monkeypatch, tmp_path, httpd)
    monkeypatch.delenv("LLMAO_HOST")
    try:
        with pytest.raises(SystemExit, match="LLMAO_HOST"):
            inst.main([])
    finally:
        httpd.shutdown()


def _reprovision(monkeypatch, tmp_path, *, fits=True):
    """A box whose last install left two units, one of them no longer in the set."""
    httpd = _config_server({})
    reloads = _box_env(monkeypatch, tmp_path, httpd)
    conf = tmp_path / "conf.d"
    conf.mkdir()
    for name in ("vllm-model-a", "vllm-dropped"):
        (conf / f"{name}.conf").write_text(f"[program:{name}]\n")
    (conf / "jupyter.conf").write_text("[program:jupyter]\n")
    events = []
    monkeypatch.setattr(inst, "supervisorctl", lambda *args: events.append(args))

    def check_fit(specs, *, data_dir):
        events.append(("check_fit",))
        return [] if fits else ["needs 34GB of VRAM, 12GB free"]

    monkeypatch.setattr(inst, "check_fit", check_fit)
    return httpd, reloads, events, conf


def test_reprovision_stops_our_servers_before_measuring_the_card(monkeypatch, tmp_path):
    httpd, reloads, events, conf = _reprovision(monkeypatch, tmp_path)
    try:
        assert inst.main([]) == 0
    finally:
        httpd.shutdown()
    # Ours only, by name, and before the fit check reads free memory.
    assert events == [("stop", "vllm-dropped", "vllm-model-a"), ("check_fit",)]
    assert sorted(p.name for p in conf.iterdir()) == ["jupyter.conf", "vllm-model-a.conf", "vllm-model-b.conf"]
    assert reloads == [True]


def test_a_refused_reprovision_leaves_the_box_serving(monkeypatch, tmp_path):
    httpd, reloads, events, conf = _reprovision(monkeypatch, tmp_path, fits=False)
    try:
        assert inst.main([]) == 1
    finally:
        httpd.shutdown()
    assert events == [
        ("stop", "vllm-dropped", "vllm-model-a"),
        ("check_fit",),
        ("start", "vllm-dropped", "vllm-model-a"),
    ]
    assert sorted(p.name for p in conf.iterdir()) == ["jupyter.conf", "vllm-dropped.conf", "vllm-model-a.conf"]
    assert reloads == []


def test_no_reload_touches_no_supervisord(monkeypatch, tmp_path):
    """RunPod: supervisord is not running yet. Stale units still go."""
    httpd, reloads, events, conf = _reprovision(monkeypatch, tmp_path)
    try:
        assert inst.main(["--no-reload"]) == 0
    finally:
        httpd.shutdown()
    assert events == [("check_fit",)]
    assert "vllm-dropped.conf" not in {p.name for p in conf.iterdir()}
    assert reloads == []


QWEN_8B = {
    "name": "qwen3-8b",
    "model": "Qwen/Qwen3-8B-FP8",
    "port": 8001,
    "api_key": "sk-x",
    "vram_gb": 9,
    "max_model_len": 40960,
    "kv_bytes_per_token": 73728,
}


def test_kv_cache_at_max_model_len():
    # What vLLM said it needed for qwen3-8b at 40960 tokens.
    assert inst.kv_gb(inst.parse_server(QWEN_8B)) == pytest.approx(2.8125)
    assert inst.kv_gb(inst.parse_server({**QWEN_8B, "kv_bytes_per_token": None})) == 0.0


def test_one_12gb_card_refuses_qwen3_8b_and_says_it_is_the_context(monkeypatch, tmp_path):
    """The weights fit; the KV cache for the context did not, and vLLM would not start."""
    _smi(monkeypatch, "11900\n")
    (problem,) = inst.check_fit([inst.parse_server(QWEN_8B)], data_dir=str(tmp_path))
    assert "11.8GB per card" in problem
    assert "2.8GB KV cache" in problem


def test_two_12gb_cards_hold_it_split_between_them(monkeypatch, tmp_path):
    _smi(monkeypatch, "11900\n11900\n")
    assert inst.gpu_count() == 2
    assert inst.check_fit([inst.parse_server(QWEN_8B)], data_dir=str(tmp_path)) == []


def test_the_smallest_card_decides(monkeypatch):
    _smi(monkeypatch, "11900\n4000\n")
    assert inst.free_vram_gb() == pytest.approx(4000 / 1024)


def test_a_server_spans_every_gpu(monkeypatch):
    monkeypatch.setattr(inst, "VLLM_BIN", "vllm")
    spec = inst.parse_server(QWEN_8B)
    assert "--tensor-parallel-size" not in inst.build_argv(spec)
    spec["tensor_parallel_size"] = 2
    argv = inst.build_argv(spec)
    assert argv[argv.index("--tensor-parallel-size") + 1] == "2"


def test_apportion_is_per_card():
    """Each card holds 1/cards of every server's weights; the rest is shared out."""
    big = {**inst.parse_server(QWEN_8B), "vram_gb": 6.0}
    small = {**inst.parse_server(QWEN_8B), "name": "small", "vram_gb": 1.0}
    # One 12GB card: 10GB usable after the 2GB reserve, 7GB of it weights.
    one = inst.apportion([dict(big), dict(small)], 12.0, cards=1)
    assert [s["gpu_memory_utilization"] for s in one] == [0.625, 0.208]
    # Two: 3.5GB of weights per card, so 6.5GB to share instead of 3.
    two = inst.apportion([dict(big), dict(small)], 12.0, cards=2)
    assert [s["gpu_memory_utilization"] for s in two] == [0.52, 0.312]


def test_a_small_card_keeps_its_reserve():
    """Two 11.63GiB RTX 3060s, one qwen3-8b: at 0.9 each card had 1.2GB left
    for CUDA, NCCL and graph capture, and vLLM ran out at startup."""
    total = 11910 / 1024
    (spec,) = inst.apportion([inst.parse_server(QWEN_8B)], total, total, cards=2)
    gmu = spec["gpu_memory_utilization"]
    assert (1 - gmu) * total >= inst.GPU_RESERVE_GB - 0.01
    # And what it claims still holds its half of the weights and of the KV cache.
    assert gmu * total - 9 / 2 >= inst.kv_gb(spec) / 2


def test_a_large_card_keeps_ten_percent():
    """The reserve is a floor: on an 80GB card 10% is already more than 2GB."""
    assert inst.usable_gb(80.0) == pytest.approx(72.0)
    assert inst.usable_gb(12.0) == pytest.approx(10.0)

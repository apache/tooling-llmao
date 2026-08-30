"""GPU-free tests for hosting/vast/env.py (mocked VastAI)."""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

if "vastai" not in sys.modules:
    fake = types.ModuleType("vastai")
    fake.VastAI = object
    sys.modules["vastai"] = fake

VAST = Path(__file__).resolve().parents[1] / "hosting" / "vast"
sys.path.insert(0, str(VAST))

import env as vast_env  # noqa: E402

EXAMPLE_MODELS = Path(__file__).resolve().parent.parent / "model_list.yaml.example"
PRIMARY_SETS = {
    "primary": [
        {"model": "gemma4-26b", "host": "127.0.0.1", "port": 8001},
        {"model": "qwen3-8b", "host": "127.0.0.1", "port": 8003},
    ]
}
FLEET_KNOBS = {
    "health_interval_s": 45,
    "health_timeout_s": 3,
    "health_grace_s": 1800,
    "health_fail_threshold": 3,
    "skew_interval_s": 180,
    "litellm_health_interval_s": 14400,
}


def test_parse_docker_env_string():
    raw = '-e FOO=bar -e BAZ=quux -p 8001:8001 -e EMPTY='
    got = vast_env.parse_docker_env(raw)
    assert got["FOO"] == "bar"
    assert got["BAZ"] == "quux"
    assert got["EMPTY"] == ""
    assert got["-p 8001:8001"] == "1"


def test_parse_docker_env_dict():
    assert vast_env.parse_docker_env({"A": 1, "-p 80:80": "1"})["A"] == "1"


def test_merge_keeps_port_keys(tmp_path, monkeypatch, capsys):
    cfg = {
        "fleet": {"key": "sk-fleet", "sets": PRIMARY_SETS, **FLEET_KNOBS},
        "models_path": str(EXAMPLE_MODELS),
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    vast = MagicMock()
    vast.show_instance.return_value = {
        "id": 42,
        "extra_env": {"KEEP": "1", "-p 8001:8001": "1"},
        "template_hash_id": "abc",
        "image_uuid": "vllm/vllm-openai:latest",
    }
    monkeypatch.setattr(vast_env, "confirm_reboot", lambda: False)

    vast_env.cmd_set(vast, 42, vllm_set="primary", config_path=cfg_path)
    kw = vast.update_instance.call_args.kwargs
    args = kw["args"]
    assert kw["image"] == "vllm/vllm-openai:latest"
    assert kw["template_hash_id"] == "abc"
    assert "env" not in kw
    assert "-p 8001:8001" in args
    assert "-e KEEP=1" in args
    assert "-e FLEET_KEY=" in args
    assert "-e VLLM_SET=primary" in args
    vast.reboot_instance.assert_not_called()
    out = capsys.readouterr().out
    assert "gemma4-26b" in out
    assert "qwen3-8b" in out


def test_unknown_set_fails(tmp_path):
    cfg = {
        "fleet": {"key": "sk-fleet", "sets": PRIMARY_SETS, **FLEET_KNOBS},
        "models_path": str(EXAMPLE_MODELS),
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    vast = MagicMock()
    with pytest.raises(SystemExit, match="unknown model_set"):
        vast_env.cmd_set(vast, 1, vllm_set="no-such-set", config_path=cfg_path)
    vast.update_instance.assert_not_called()


def test_change_me_fleet_key_fails(tmp_path):
    cfg = {
        "fleet": {"key": "CHANGE_ME_FLEET_KEY"},
        "models_path": str(EXAMPLE_MODELS),
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(SystemExit, match="fleet.key"):
        vast_env.cmd_set(MagicMock(), 1, vllm_set="primary", config_path=cfg_path)


def test_vllm_set_required():
    with pytest.raises(SystemExit):
        vast_env.build_parser().parse_args(["set", "1"])


def test_reboot_yes(tmp_path, monkeypatch):
    cfg = {
        "fleet": {"key": "sk-fleet", "sets": PRIMARY_SETS, **FLEET_KNOBS},
        "models_path": str(EXAMPLE_MODELS),
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    vast = MagicMock()
    vast.show_instance.return_value = {
        "id": 9,
        "extra_env": {},
        "image_uuid": "vllm/vllm-openai:latest",
    }
    monkeypatch.setattr(vast_env, "confirm_reboot", lambda: True)
    vast_env.cmd_set(vast, 9, vllm_set="primary", config_path=cfg_path)
    vast.reboot_instance.assert_called_once_with(id=9)


def test_confirm_reboot_default_yes(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert vast_env.confirm_reboot() is True


def test_confirm_reboot_n(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert vast_env.confirm_reboot() is False


def test_list_header_fleet_and_set(capsys):
    vast = MagicMock()
    vast.show_instances.return_value = [
        {
            "id": 10,
            "label": "box-a",
            "actual_status": "running",
            "template_hash_id": "abc",
            "extra_env": {"FLEET_KEY": "secret", "VLLM_SET": "primary"},
        },
        {
            "id": 11,
            "label": "box-b",
            "actual_status": "exited",
            "template_hash_id": "def",
            "extra_env": {},
            "image_args": "-e FLEET_KEY=from-args -e VLLM_SET=llmao-1",
        },
    ]
    vast_env.cmd_list(vast)
    out = capsys.readouterr().out.splitlines()
    header = out[0]
    assert header.index("ID") < header.index("STATUS") < header.index("FLEET")
    assert "VLLM_SET" in header and "TEMPLATE" in header and "LABEL" in header
    assert "\t" not in header
    # columns stay aligned across rows
    fleet_at = header.index("FLEET")
    set_at = header.index("VLLM_SET")
    assert out[1][fleet_at] == "✓"
    assert out[1][set_at:].startswith("primary")
    assert "secret" not in out[1]
    assert out[2][fleet_at] == "✓"
    assert "llmao-1" in out[2]
    assert "from-args" not in out[2]


def test_default_command_is_list():
    args = vast_env.build_parser().parse_args([])
    assert args.cmd == "list"


def test_set_requires_image(tmp_path):
    cfg = {
        "fleet": {"key": "sk-fleet", "sets": PRIMARY_SETS, **FLEET_KNOBS},
        "models_path": str(EXAMPLE_MODELS),
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    vast = MagicMock()
    vast.show_instance.return_value = {"id": 1, "extra_env": {}}
    with pytest.raises(SystemExit, match="image_uuid"):
        vast_env.cmd_set(vast, 1, vllm_set="primary", config_path=cfg_path, confirm=lambda: False)
    vast.update_instance.assert_not_called()


def test_set_http_400_prints_body(tmp_path, monkeypatch):
    cfg = {
        "fleet": {"key": "sk-fleet", "sets": PRIMARY_SETS, **FLEET_KNOBS},
        "models_path": str(EXAMPLE_MODELS),
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    vast = MagicMock()
    vast.show_instance.return_value = {
        "id": 1,
        "extra_env": {},
        "image_uuid": "vllm/vllm-openai:latest",
    }
    err = RuntimeError("400")
    err.response = SimpleNamespace(status_code=400, text='{"msg":"env must be a string"}')
    vast.update_instance.side_effect = err
    with pytest.raises(SystemExit, match="Vast HTTP 400:.*env must be a string"):
        vast_env.cmd_set(vast, 1, vllm_set="primary", config_path=cfg_path, confirm=lambda: False)

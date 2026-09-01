"""config_for_host: JSON for a client IP from fleet.hosts + catalog."""
from pathlib import Path

import pytest
import yaml
from easydict import EasyDict as edict

from llmao.fleet import (
    UnknownHost,
    config_for_host,
    normalize_peer_ip,
    validate_fleet,
)
from llmao.models import load_model_list

EXAMPLE = Path(__file__).resolve().parent.parent / "model_list.yaml.example"
EXAMPLE_CFG = Path(__file__).resolve().parent.parent / "config.yaml.example"

FLEET_KNOBS = {
    "health_interval_s": 45,
    "health_timeout_s": 3,
    "health_grace_s": 1800,
    "health_fail_threshold": 3,
    "skew_interval_s": 180,
    "litellm_health_interval_s": 14400,
}


def _cfg(hosts, models_path=EXAMPLE):
    return edict({
        "fleet": {"hosts": hosts, **FLEET_KNOBS},
        "models_path": str(models_path),
    })


def test_example_primary_host():
    cfg = edict(yaml.safe_load(EXAMPLE_CFG.read_text(encoding="utf-8")))
    models = load_model_list(EXAMPLE)
    validate_fleet(cfg, models=models)
    payload = config_for_host("127.0.0.1", models=models, cfg=cfg)
    assert payload["host"] == "127.0.0.1"
    assert "set_id" not in payload
    names = [s["name"] for s in payload["servers"]]
    assert "gemma4-26b" in names
    assert "qwen3-8b" in names
    gemma = next(s for s in payload["servers"] if s["name"] == "gemma4-26b")
    assert gemma["model"] == "google/gemma-4-26B-A4B-it"
    assert gemma["host"] == "127.0.0.1"
    assert gemma["port"] == 8001


def test_unknown_host():
    cfg = _cfg({"127.0.0.1": [["gemma4-26b", 8001]]})
    models = load_model_list(EXAMPLE)
    validate_fleet(cfg, models=models)
    with pytest.raises(UnknownHost):
        config_for_host("10.0.0.9", models=models, cfg=cfg)


def test_optional_name_two_copies():
    cfg = _cfg({
        "10.0.0.1": [
            ["qwen3-8b", 8003],
            ["qwen3-8b", 8004, "qwen3-8b-b"],
        ]
    })
    models = load_model_list(EXAMPLE)
    validate_fleet(cfg, models=models)
    payload = config_for_host("10.0.0.1", models=models, cfg=cfg)
    assert [s["name"] for s in payload["servers"]] == ["qwen3-8b", "qwen3-8b-b"]
    assert [s["port"] for s in payload["servers"]] == [8003, 8004]


def test_duplicate_name():
    cfg = _cfg({
        "10.0.0.1": [
            ["qwen3-8b", 8003],
            ["qwen3-8b", 8004],
        ]
    })
    with pytest.raises(ValueError, match="duplicate name"):
        validate_fleet(cfg, models=load_model_list(EXAMPLE))


def test_duplicate_port():
    cfg = _cfg({
        "10.0.0.1": [
            ["gemma4-26b", 8001],
            ["qwen3-8b", 8001],
        ]
    })
    with pytest.raises(ValueError, match="duplicate port"):
        validate_fleet(cfg, models=load_model_list(EXAMPLE))


def test_unknown_model():
    cfg = _cfg({"10.0.0.1": [["nope", 9]]})
    with pytest.raises(ValueError, match="unknown model"):
        validate_fleet(cfg, models=load_model_list(EXAMPLE))


def test_normalize_peer_ip():
    assert normalize_peer_ip("::ffff:203.0.113.10") == "203.0.113.10"
    assert normalize_peer_ip("203.0.113.10") == "203.0.113.10"

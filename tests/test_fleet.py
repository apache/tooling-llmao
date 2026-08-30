"""config_for_set: JSON for a set from fleet.sets joined to the catalog."""
from pathlib import Path

import pytest
import yaml
from easydict import EasyDict as edict

from llmao.fleet import UnknownSet, config_for_set, validate_fleet
from llmao.models import load_model_list

EXAMPLE = Path(__file__).resolve().parent.parent / "model_list.yaml.example"
EXAMPLE_CFG = Path(__file__).resolve().parent.parent / "config.yaml.example"


def _cfg(sets, models_path=EXAMPLE):
    return edict({
        "fleet": {"sets": sets},
        "models_path": str(models_path),
    })


def test_example_primary_set():
    cfg = edict(yaml.safe_load(EXAMPLE_CFG.read_text(encoding="utf-8")))
    models = load_model_list(EXAMPLE)
    validate_fleet(cfg, models=models)
    payload = config_for_set("primary", models=models, cfg=cfg)
    assert payload["set_id"] == "primary"
    names = [s["name"] for s in payload["servers"]]
    assert "gemma4-26b" in names
    assert "qwen3-8b" in names
    gemma = next(s for s in payload["servers"] if s["name"] == "gemma4-26b")
    assert gemma["model"] == "google/gemma-4-26B-A4B-it"
    assert gemma["host"] == "127.0.0.1"
    assert gemma["port"] == 8001
    assert gemma["api_key"]
    qwen = next(s for s in payload["servers"] if s["name"] == "qwen3-8b")
    assert "--reasoning-parser" in qwen["args"]


def test_unknown_set():
    cfg = _cfg({"primary": [{"model": "gemma4-26b", "host": "127.0.0.1", "port": 8001}]})
    models = load_model_list(EXAMPLE)
    validate_fleet(cfg, models=models)
    with pytest.raises(UnknownSet):
        config_for_set("no-such-set", models=models, cfg=cfg)


def test_missing_vllm_block():
    models = [edict({
        "model_name": "x",
        "litellm_params": {"api_key": "sk-x"},
        "model_info": {},
    })]
    cfg = _cfg({"s": [{"model": "x", "host": "10.0.0.1", "port": 9}]})
    with pytest.raises(ValueError, match="model_info.vllm"):
        validate_fleet(cfg, models=models)


def test_unknown_model():
    cfg = _cfg({"s": [{"model": "nope", "host": "10.0.0.1", "port": 9}]})
    with pytest.raises(ValueError, match="unknown model"):
        validate_fleet(cfg, models=load_model_list(EXAMPLE))


def test_missing_host():
    cfg = _cfg({"s": [{"model": "gemma4-26b", "port": 8001}]})
    with pytest.raises(ValueError, match="model, host, and port"):
        validate_fleet(cfg, models=load_model_list(EXAMPLE))


def test_two_copies_different_ports():
    cfg = _cfg({
        "dual": [
            {"model": "gemma4-26b", "host": "10.0.0.1", "port": 8001},
            {"model": "gemma4-26b", "host": "10.0.0.1", "port": 8011, "name": "gemma4-26b-b"},
        ]
    })
    models = load_model_list(EXAMPLE)
    validate_fleet(cfg, models=models)
    payload = config_for_set("dual", models=models, cfg=cfg)
    assert [s["name"] for s in payload["servers"]] == ["gemma4-26b", "gemma4-26b-b"]
    assert [s["port"] for s in payload["servers"]] == [8001, 8011]
    assert payload["servers"][0]["model"] == payload["servers"][1]["model"]


def test_duplicate_name():
    cfg = _cfg({
        "s": [
            {"model": "gemma4-26b", "host": "10.0.0.1", "port": 1},
            {"model": "gemma4-26b", "host": "10.0.0.1", "port": 2},
        ]
    })
    with pytest.raises(ValueError, match="duplicate name"):
        validate_fleet(cfg, models=load_model_list(EXAMPLE))


def test_duplicate_host_port():
    cfg = _cfg({
        "s": [
            {"model": "gemma4-26b", "host": "10.0.0.1", "port": 8001, "name": "a"},
            {"model": "qwen3-8b", "host": "10.0.0.1", "port": 8001, "name": "b"},
        ]
    })
    with pytest.raises(ValueError, match="duplicate"):
        validate_fleet(cfg, models=load_model_list(EXAMPLE))


def test_catalog_has_no_port():
    for model in load_model_list(EXAMPLE):
        vllm = model.model_info.vllm
        assert "port" not in vllm
        assert "model_set" not in model.model_info

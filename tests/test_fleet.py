"""config_for_set: JSON for a set from config.yaml fleet.sets + catalog."""
from pathlib import Path

import pytest
import yaml
from easydict import EasyDict as edict

from llmao.fleet import UnknownSet, config_for_set
from llmao.models import load_model_list

EXAMPLE = Path(__file__).resolve().parent.parent / "model_list.yaml.example"
EXAMPLE_CFG = Path(__file__).resolve().parent.parent / "config.yaml.example"


def _cfg(sets, entries_path=EXAMPLE):
    return edict({
        "fleet": {"sets": sets},
        "models_path": str(entries_path),
    })


def test_example_primary_set():
    cfg = edict(yaml.safe_load(EXAMPLE_CFG.read_text(encoding="utf-8")))
    payload = config_for_set("primary", entries=load_model_list(EXAMPLE), cfg=cfg)
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
    with pytest.raises(UnknownSet):
        config_for_set("no-such-set", entries=load_model_list(EXAMPLE), cfg=cfg)


def test_missing_vllm_block():
    entries = [{
        "model_name": "x",
        "litellm_params": {"api_key": "sk-x"},
        "model_info": {},
    }]
    cfg = _cfg({"s": [{"model": "x", "host": "10.0.0.1", "port": 9}]})
    with pytest.raises(ValueError, match="model_info.vllm"):
        config_for_set("s", entries=entries, cfg=cfg)


def test_unknown_catalog_model():
    cfg = _cfg({"s": [{"model": "nope", "host": "10.0.0.1", "port": 9}]})
    with pytest.raises(ValueError, match="unknown model"):
        config_for_set("s", entries=load_model_list(EXAMPLE), cfg=cfg)


def test_missing_host():
    cfg = _cfg({"s": [{"model": "gemma4-26b", "port": 8001}]})
    with pytest.raises(ValueError, match="model, host, and port"):
        config_for_set("s", entries=load_model_list(EXAMPLE), cfg=cfg)


def test_two_copies_different_ports():
    cfg = _cfg({
        "dual": [
            {"model": "gemma4-26b", "host": "10.0.0.1", "port": 8001},
            {"model": "gemma4-26b", "host": "10.0.0.1", "port": 8011, "name": "gemma4-26b-b"},
        ]
    })
    payload = config_for_set("dual", entries=load_model_list(EXAMPLE), cfg=cfg)
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
        config_for_set("s", entries=load_model_list(EXAMPLE), cfg=cfg)


def test_duplicate_host_port():
    cfg = _cfg({
        "s": [
            {"model": "gemma4-26b", "host": "10.0.0.1", "port": 8001, "name": "a"},
            {"model": "qwen3-8b", "host": "10.0.0.1", "port": 8001, "name": "b"},
        ]
    })
    with pytest.raises(ValueError, match="duplicate"):
        config_for_set("s", entries=load_model_list(EXAMPLE), cfg=cfg)


def test_catalog_has_no_port():
    for entry in load_model_list(EXAMPLE):
        vllm = (entry.get("model_info") or {}).get("vllm") or {}
        assert "port" not in vllm
        assert "model_set" not in (entry.get("model_info") or {})

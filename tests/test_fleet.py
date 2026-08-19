"""config_for_set: JSON for a model_set from model_list entries."""
from pathlib import Path

import pytest

from llmao.fleet import UnknownSet, config_for_set
from llmao.models import load_model_list

EXAMPLE = Path(__file__).resolve().parent.parent / "model_list.yaml.example"


def test_example_primary_set():
    payload = config_for_set("primary", entries=load_model_list(EXAMPLE))
    assert payload["set_id"] == "primary"
    names = [s["name"] for s in payload["servers"]]
    assert "gemma4-26b" in names
    assert "qwen3-8b" in names
    gemma = next(s for s in payload["servers"] if s["name"] == "gemma4-26b")
    assert gemma["model"] == "google/gemma-4-26B-A4B-it"
    assert gemma["port"] == 8001
    assert gemma["api_key"]
    qwen = next(s for s in payload["servers"] if s["name"] == "qwen3-8b")
    assert "--reasoning-parser" in qwen["args"]


def test_unknown_set():
    with pytest.raises(UnknownSet):
        config_for_set("no-such-set", entries=load_model_list(EXAMPLE))


def test_missing_vllm_block():
    entries = [{
        "model_name": "x",
        "litellm_params": {"api_key": "sk-x"},
        "model_info": {"model_set": "s"},
    }]
    with pytest.raises(ValueError, match="model_info.vllm"):
        config_for_set("s", entries=entries)

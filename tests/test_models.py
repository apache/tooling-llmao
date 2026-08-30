"""Model inventory: shared YAML + LiteLLM model_info extras."""
from __future__ import annotations

import pathlib
import tempfile
import textwrap

import pytest
import yaml
from easydict import EasyDict as edict
from litellm.types.router import Deployment

from llmao.models import load_model_list, public_models, models_path_from_cfg, ux_models

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "model_list.yaml.example"


def test_example_model_list_loads_for_ux():
    models = load_model_list(EXAMPLE)
    assert len(models) >= 2
    names = {m.model_name for m in models}
    assert models[0].model_info.vllm.model
    assert "gemma4-26b" in names
    assert "qwen3-8b" in names
    pub = public_models(EXAMPLE)
    for p in pub:
        assert "model_name" in p
        assert "api_key" not in p
        assert "api_base" not in p
        assert "litellm_params" not in p
        assert p.get("license")
        assert p.get("openness")


def test_missing_model_list_fails_fast():
    missing = ROOT / "model_list.yaml.does-not-exist"
    with pytest.raises(FileNotFoundError, match="model_list.yaml.example"):
        load_model_list(missing)


def test_models_path_from_cfg():
    cfg = edict({"models_path": "model_list.yaml.example"})
    p = models_path_from_cfg(cfg)
    assert p.name == "model_list.yaml.example"
    assert p.is_file()


def test_ux_models_redacts_supply_path_for_non_admins():
    redacted = ux_models(EXAMPLE, reveal_supply=False)
    full = ux_models(EXAMPLE, reveal_supply=True)
    assert len(redacted) >= 2
    assert len(full) == len(redacted)
    for r in redacted:
        assert r["model_name"]
        assert r["display_name"]
        assert r["provider"] == ""
        assert r["weights_distribution"] == ""
        assert r["hosting_label"] in ("Self-hosted", "External", "—")
        # Free-text flattened for data-* attributes (no raw newlines).
        assert "\n" not in r["notes"]
    for f in full:
        # Example inventory includes weights_distribution for self-host models.
        assert f.get("weights_distribution") or f.get("provider")
        assert "\n" not in f["notes"]


def test_litellm_deployment_preserves_model_info_extras():
    """Regression: LiteLLM Deployment/ModelInfo must accept ASF metadata fields.

    Inventory UX stores governance fields under model_info (flat). If a future
    litellm version rejects extras, this test fails and we must revisit.
    """
    entry = {
        "model_name": "gemma4-26b",
        "litellm_params": {
            "model": "openai/gemma4-26b",
            "api_base": "http://127.0.0.1:8001",
            "api_key": "sk-test",
        },
        "model_info": {
            "display_name": "Gemma test",
            "license": "Apache-2.0",
            "openness": "open-weight",
            "weights_distribution": "google/gemma",
            "training_data_provenance": "undisclosed",
            "provenance_record": "absent",
            "self_hosted": True,
            "supports_thinking": True,
            "thinks_by_default": False,
            "notes": "regression probe",
        },
    }
    dep = Deployment(**entry)
    info = dep.model_info
    assert info is not None
    assert getattr(info, "display_name", None) == "Gemma test"
    assert getattr(info, "license", None) == "Apache-2.0"
    assert getattr(info, "openness", None) == "open-weight"
    assert getattr(info, "provenance_record", None) == "absent"
    assert getattr(info, "self_hosted", None) is True
    assert getattr(info, "supports_thinking", None) is True


def test_roundtrip_yaml_through_deployment():
    """Example inventory entries must construct as LiteLLM Deployments."""
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    for entry in data["model_list"]:
        # Deployment requires api_key etc.; example has CHANGE_ME placeholders — fine.
        dep = Deployment(**entry)
        assert dep.model_name
        assert dep.model_info is not None
        assert getattr(dep.model_info, "license", None)

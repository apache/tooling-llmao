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

"""Model inventory: shared YAML + LiteLLM model_info extras."""

from __future__ import annotations

import pathlib

import pytest
import yaml
from easydict import EasyDict
from litellm.types.router import Deployment

from llmao.models import (
    load_models,
    model_available_for,
    model_in_service,
    models_path_from_cfg,
    public_models,
    ux_models,
    validate_models,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "models.yaml"


def test_models_yaml_loads_for_ux():
    models = load_models(EXAMPLE)
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


def test_missing_models_yaml_fails_fast():
    missing = ROOT / "models.yaml.does-not-exist"
    with pytest.raises(FileNotFoundError, match="Missing"):
        load_models(missing)


def test_models_path_from_cfg():
    cfg = EasyDict({"models_path": "models.yaml"})
    p = models_path_from_cfg(cfg)
    assert p.name == "models.yaml"
    assert p.is_file()


def test_ux_models_redacts_supply_path_for_non_admins():
    redacted = ux_models(EXAMPLE, reveal_supply=False)
    full = ux_models(EXAMPLE, reveal_supply=True)
    assert len(redacted) >= 2
    assert len(full) == len(redacted)
    for r in redacted:
        assert r["model_name"]
        assert r["display_name"]
        assert r["weights_distribution"] == ""
        assert r["hosting_label"] in ("Self-hosted", "External")
        assert model_available_for(None, r) is True
        # Free-text flattened for data-* attributes (no raw newlines).
        assert "\n" not in r["notes"]
    for f in full:
        # Example inventory includes weights_distribution for self-host models.
        assert f.get("weights_distribution")
        assert "\n" not in f["notes"]


def test_definitions_require_self_hosted():
    models = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["models"]
    row = EasyDict(models[0])
    del row.model_info["self_hosted"]
    with pytest.raises(ValueError, match="self_hosted"):
        validate_models([row])


def test_commercial_requires_api_base():
    row = EasyDict(
        {
            "model_name": "paid-model",
            "litellm_params": {"model": "openai/gpt-4"},
            "model_info": {"self_hosted": False, "license": "proprietary"},
        }
    )
    with pytest.raises(ValueError, match="api_base"):
        validate_models([row])
    row.litellm_params.api_base = "https://api.openai.com/v1"
    validate_models([row])


def test_model_in_service():
    assert model_in_service("up", self_hosted=True) is True
    assert model_in_service("mixed", self_hosted=True) is True
    assert model_in_service("starting", self_hosted=True) is False
    assert model_in_service("down", self_hosted=True) is False
    assert model_in_service("", self_hosted=True) is False
    assert model_in_service("", self_hosted=False) is True


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
    """models.yaml rows must construct as LiteLLM Deployments."""
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    for entry in data["models"]:
        # Deployment requires api_key etc.; example has CHANGE_ME placeholders — fine.
        dep = Deployment(**entry)
        assert dep.model_name
        assert dep.model_info is not None
        assert getattr(dep.model_info, "license", None)

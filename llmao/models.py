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

"""Load admin model definitions (models.yaml).

UX, vLLM recipes, and the template for POST /model/new. LiteLLM does not
include this file. Fail-fast if missing.
"""

from __future__ import annotations

import pathlib
from typing import Any

import ezt
import yaml
from easydict import EasyDict

# Default path: repo / install root (next to main.py).
DEFAULT_MODELS_PATH = pathlib.Path(__file__).resolve().parent.parent / "models.yaml"


def models_path_from_cfg(cfg: Any = None) -> pathlib.Path:
    """Resolve models.yaml path from APP.cfg.models_path or the default."""
    if cfg is not None and getattr(cfg, "models_path", None):
        p = pathlib.Path(cfg.models_path)
        if not p.is_absolute():
            p = pathlib.Path(__file__).resolve().parent.parent / p
        return p
    return DEFAULT_MODELS_PATH


def load_models(path: pathlib.Path | None = None, *, cfg: Any = None) -> list[dict[str, Any]]:
    """Load models.yaml. Raises FileNotFoundError if absent."""
    path = path or models_path_from_cfg(cfg)
    if not path.is_file():
        raise FileNotFoundError(f"Missing {path} (admin model definitions).")
    data = EasyDict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    if "models" not in data or not isinstance(data.models, list):
        raise ValueError(f"{path}: expected top-level models: [ ... ]")
    validate_models(data.models, path=path)
    return data.models


def validate_models(models: list, *, path: pathlib.Path | str = "models.yaml") -> None:
    """Fail-fast: self_hosted boolean; vLLM recipe vs commercial api_base."""
    seen: set[str] = set()
    for model in models:
        if "model_name" not in model or not model.model_name:
            raise ValueError(f"{path}: model missing model_name")
        name = str(model.model_name)
        if name in seen:
            raise ValueError(f"{path}: duplicate model_name {name}")
        seen.add(name)
        if "litellm_params" not in model:
            raise ValueError(f"{name}: litellm_params is required")
        if "model_info" not in model:
            raise ValueError(f"{name}: model_info is required")
        info = model.model_info
        flag = info.get("self_hosted") if hasattr(info, "get") else None
        if not isinstance(flag, bool):
            raise ValueError(f"{name}: model_info.self_hosted must be true or false")
        if flag:
            if "vllm" not in info:
                raise ValueError(f"{name}: model_info.vllm is required when self_hosted")
            vllm = info.vllm
            if "model" not in vllm or not vllm.model:
                raise ValueError(f"{name}: model_info.vllm.model is required")
        else:
            params = model.litellm_params
            base = params.get("api_base") if hasattr(params, "get") else None
            if not str(base or "").strip():
                raise ValueError(f"{name}: self_hosted false requires litellm_params.api_base")


def public_models(path: pathlib.Path | None = None, *, cfg: Any = None) -> list[dict[str, Any]]:
    """Models for UX: model_name + model_info (no litellm_params / secrets)."""
    out = []
    for entry in load_models(path, cfg=cfg):
        name = entry.get("model_name")
        info = dict(entry.get("model_info") or {})
        # Never surface api credentials or api_base on the Models page.
        out.append({"model_name": name, **info})
    return out


# Fields that describe how/where we obtain or serve a model (partnerships, HF paths).
_SUPPLY_PATH_KEYS = frozenset(
    {
        "weights_distribution",
        "training_data_provenance",
        "provenance_record",
    }
)


def _oneline(s: Any) -> str:
    """Collapse whitespace so values stay safe in single-line HTML attributes."""
    if s is None or s is False:
        return ""
    return " ".join(str(s).split())


def _hosting_label(m: dict[str, Any]) -> str:
    """Public hosting class from the required self_hosted boolean."""
    if m.get("self_hosted") is True:
        return "Self-hosted"
    if m.get("self_hosted") is False:
        return "External"
    return "—"


def model_available_for(identity: Any, model: dict[str, Any]) -> bool:
    """Policy: whether this user is allowed this model.

    Always True until team allow-lists, envelope, and other gates exist
    (STATUS P5). Combine with ``model_in_service`` for the Available column.
    """
    return True


def model_in_service(health: str, *, self_hosted: bool) -> bool:
    """Whether this model can take traffic right now.

    ``health`` is Fleet.model_health: up / starting / down / mixed / empty.
    Self-hosted with no serving replica is not in service. Vendor models
    (not self-hosted, empty health) are treated as in service.
    """
    if health in ("up", "mixed"):
        return True
    if not health:
        return not self_hosted
    return False


def ux_models(
    path: pathlib.Path | None = None,
    *,
    cfg: Any = None,
    reveal_supply: bool = False,
) -> list[dict[str, Any]]:
    """Shape rows for the Models page (table + detail modal).

    When ``reveal_supply`` is False (normal committers), omit fields that
    describe procurement, weight paths, or commercial partnerships.

    Free-text is collapsed to one line for HTML data-* attributes (EZT
    HTML-escapes quotes; embedded newlines still break attributes).

    TODO: return EasyDict rows (dotted access) instead of plain dicts.
    """
    rows = []
    for m in public_models(path, cfg=cfg):
        if not reveal_supply:
            m = {k: v for k, v in m.items() if k not in _SUPPLY_PATH_KEYS}
        name = _oneline(m.get("model_name"))
        display = _oneline(m.get("display_name")) or name
        ctx = m.get("context_window")
        rows.append(
            {
                "model_name": name,
                "display_name": display,
                "hosting_label": _hosting_label(m),
                "self_hosted": ezt.boolean(m.get("self_hosted")),
                "context_window": ctx if ctx is not None else "—",
                "license": _oneline(m.get("license")) or "—",
                "modality": _oneline(m.get("modality")),
                "supports_thinking": ezt.boolean(m.get("supports_thinking")),
                "thinks_by_default": ezt.boolean(m.get("thinks_by_default")),
                "openness": _oneline(m.get("openness")),
                "notes": _oneline(m.get("notes")),
                "reveal_supply": reveal_supply,
                # Admin-only supply fields (empty strings when redacted)
                "weights_distribution": (_oneline(m.get("weights_distribution")) if reveal_supply else ""),
                "training_data_provenance": (_oneline(m.get("training_data_provenance")) if reveal_supply else ""),
                "provenance_record": (_oneline(m.get("provenance_record")) if reveal_supply else ""),
            }
        )
    return rows

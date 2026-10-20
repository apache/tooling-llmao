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

UX, vLLM recipes, and the template for registering our own boxes with
LiteLLM (POST /model/new). Not where a model is served: that is each
deployment's (fleet.hosts, config.yaml providers). LiteLLM does not include
this file. Fail-fast if missing.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any

import ezt
import yaml
from easydict import EasyDict as edict  # noqa: N813

_LOGGER = logging.getLogger(__name__)

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
    data = edict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    if "models" not in data or not isinstance(data.models, list):
        raise ValueError(f"{path}: expected top-level models: [ ... ]")
    validate_models(data.models, path=path)
    return data.models


def vllm_recipe(model: Any) -> Any:
    """How to run this model on one of our own boxes, or None if it says nothing."""
    info = model.get("model_info") if hasattr(model, "get") else None
    return (info.get("vllm") if hasattr(info, "get") else None) or None


def served_model_name(model: Any) -> str:
    """The name vLLM must answer to: litellm_params.model after its provider prefix.

    LiteLLM strips "hosted_vllm/" and sends the rest as the request's model,
    so a box serving under any other name -- vLLM's default is the Hugging
    Face id -- answers every request with 404. Taken from the one field
    LiteLLM reads, so the two cannot drift apart.
    """
    return str(model.litellm_params.model).partition("/")[2] or str(model.litellm_params.model)


# Keys models.yaml no longer reads. Where a model is served, and with what
# credentials, belongs to a deployment: fleet.hosts for our boxes, config.yaml
# providers for anyone we pay. (section, key, where it went)
DEPRECATED_KEYS = (
    ("litellm_params", "api_base", "a provider's endpoint is config.yaml providers.<name>.api_base"),
    ("litellm_params", "api_key", "a provider's key is config.yaml providers.<name>.api_key"),
    ("model_info", "provider", "the provider is a deployment's: fleet.hosts or config.yaml providers"),
    ("model_info", "self_hosted", "where a model runs is a deployment's: fleet.hosts or config.yaml providers"),
)


def warn_deprecated(models: list) -> None:
    """Log each deprecated key. They are ignored, not refused. Call at startup."""
    for model in models:
        for section, key, instead in DEPRECATED_KEYS:
            if key in (model.get(section) or {}):
                _LOGGER.warning("models.yaml: %s: %s.%s is ignored; %s", model.model_name, section, key, instead)


def validate_models(models: list, *, path: pathlib.Path | str = "models.yaml") -> None:
    """Fail-fast on what a model must carry.

    A model says what it is, and how to run it on our boxes if we can. It
    does not say where it is served: that is each deployment's, from
    fleet.hosts or config.yaml providers. A model with neither simply has
    no deployments.
    """
    seen: set[str] = set()
    for model in models:
        if "model_name" not in model or not model.model_name:
            raise ValueError(f"{path}: model missing model_name")
        name = str(model.model_name)
        if name in seen:
            raise ValueError(f"{path}: duplicate model_name {name}")
        seen.add(name)
        if "model_info" not in model:
            raise ValueError(f"{name}: model_info is required")
        recipe = vllm_recipe(model)
        if recipe is None:
            continue
        if "model" not in recipe or not recipe.model:
            raise ValueError(f"{name}: model_info.vllm.model is required")
        # The template for registering one of our boxes: hosted_vllm/<name>.
        if not (model.get("litellm_params") or {}).get("model"):
            raise ValueError(f"{name}: litellm_params.model is required to serve it from our boxes")


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


def model_available_for(identity: Any, model: dict[str, Any]) -> bool:
    """Policy: whether this user is allowed this model.

    Always True until team allow-lists, envelope, and other gates exist
    (STATUS P5). Combine with ``model_in_service`` for the Available column.
    """
    return True


def model_in_service(health: str) -> bool:
    """Whether this model can take traffic right now.

    ``health`` is the model_status roll-up: available, degraded, or unavailable.
    Available and Degraded have at least one fresh Healthy deployment.
    """
    return health in ("available", "degraded")


def ux_models(
    path: pathlib.Path | None = None,
    *,
    cfg: Any = None,
    reveal_supply: bool = False,
) -> list[edict]:
    """Shape rows for the Models page (table + detail modal).

    When ``reveal_supply`` is False (normal committers), omit fields that
    describe procurement, weight paths, or commercial partnerships.

    Free-text is collapsed to one line for HTML data-* attributes (EZT
    HTML-escapes quotes; embedded newlines still break attributes).
    """
    rows = []
    for m in public_models(path, cfg=cfg):
        if not reveal_supply:
            m = {k: v for k, v in m.items() if k not in _SUPPLY_PATH_KEYS}
        name = _oneline(m.get("model_name"))
        display = _oneline(m.get("display_name")) or name
        ctx = m.get("context_window")
        rows.append(
            edict(
                model_name=name,
                display_name=display,
                good_for=_oneline(m.get("good_for")),
                context_window=ctx if ctx is not None else "—",
                license=_oneline(m.get("license")) or "—",
                modality=_oneline(m.get("modality")),
                supports_thinking=ezt.boolean(m.get("supports_thinking")),
                thinks_by_default=ezt.boolean(m.get("thinks_by_default")),
                openness=_oneline(m.get("openness")),
                notes=_oneline(m.get("notes")),
                reveal_supply=reveal_supply,
                # Admin-only supply fields (empty strings when redacted)
                weights_distribution=(_oneline(m.get("weights_distribution")) if reveal_supply else ""),
                training_data_provenance=(_oneline(m.get("training_data_provenance")) if reveal_supply else ""),
                provenance_record=(_oneline(m.get("provenance_record")) if reveal_supply else ""),
            )
        )
    return rows

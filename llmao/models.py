"""Load the shared model inventory (model_list.yaml).

Same file LiteLLM includes via litellm.yaml. Fail-fast if missing — same
presumption as config.yaml and litellm.yaml (copy from *.example).
"""
from __future__ import annotations

import pathlib
from typing import Any, Dict, List, Optional

import yaml

# Default path: repo / install root (next to main.py).
DEFAULT_MODELS_PATH = pathlib.Path(__file__).resolve().parent.parent / "model_list.yaml"


def models_path_from_cfg(cfg: Any = None) -> pathlib.Path:
    """Resolve model_list path from APP.cfg.models_path or the default."""
    if cfg is not None and getattr(cfg, "models_path", None):
        p = pathlib.Path(cfg.models_path)
        if not p.is_absolute():
            p = pathlib.Path(__file__).resolve().parent.parent / p
        return p
    return DEFAULT_MODELS_PATH


def load_model_list(path: Optional[pathlib.Path] = None, *, cfg: Any = None) -> List[Dict[str, Any]]:
    """Load model_list from YAML. Raises FileNotFoundError if absent."""
    path = path or models_path_from_cfg(cfg)
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing {path}. Copy model_list.yaml.example to model_list.yaml "
            f"(same pattern as config.yaml and litellm.yaml)."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    models = data.get("model_list")
    if not isinstance(models, list):
        raise ValueError(f"{path}: expected top-level model_list: [ ... ]")
    return models


def public_models(path: Optional[pathlib.Path] = None, *, cfg: Any = None) -> List[Dict[str, Any]]:
    """Models for UX: model_name + model_info (no litellm_params / secrets)."""
    out = []
    for entry in load_model_list(path, cfg=cfg):
        name = entry.get("model_name")
        info = dict(entry.get("model_info") or {})
        # Never surface api credentials or api_base in the public catalog.
        out.append({"model_name": name, **info})
    return out

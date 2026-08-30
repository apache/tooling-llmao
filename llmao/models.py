"""Load the shared model inventory (model_list.yaml).

Same file LiteLLM includes via litellm.yaml. Fail-fast if missing — same
presumption as config.yaml and litellm.yaml (copy from *.example).
"""
from __future__ import annotations

import pathlib
from typing import Any, Dict, List, Optional

import ezt
import yaml
from easydict import EasyDict as edict

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
    data = edict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    if "model_list" not in data or not isinstance(data.model_list, list):
        raise ValueError(f"{path}: expected top-level model_list: [ ... ]")
    return data.model_list


def public_models(path: Optional[pathlib.Path] = None, *, cfg: Any = None) -> List[Dict[str, Any]]:
    """Models for UX: model_name + model_info (no litellm_params / secrets)."""
    out = []
    for entry in load_model_list(path, cfg=cfg):
        name = entry.get("model_name")
        info = dict(entry.get("model_info") or {})
        # Never surface api credentials or api_base in the catalog UX.
        out.append({"model_name": name, **info})
    return out


# Fields that describe how/where we obtain or serve a model (partnerships, HF paths).
_SUPPLY_PATH_KEYS = frozenset({
    "weights_distribution",
    "training_data_provenance",
    "provenance_record",
    "provider",  # may name commercial partners; generic hosting badge is separate
})


def _oneline(s: Any) -> str:
    """Collapse whitespace so values stay safe in single-line HTML attributes."""
    if s is None or s is False:
        return ""
    return " ".join(str(s).split())


def _hosting_label(m: Dict[str, Any]) -> str:
    """Public hosting class (not partnership detail). Prefer self_hosted flag."""
    if m.get("self_hosted") is True:
        return "Self-hosted"
    if m.get("self_hosted") is False:
        return "External"
    prov = (m.get("provider") or "").lower()
    if prov in ("self-host", "selfhost", "self-hosted"):
        return "Self-hosted"
    if prov:
        return "External"
    return "—"


def ux_models(
    path: Optional[pathlib.Path] = None,
    *,
    cfg: Any = None,
    reveal_supply: bool = False,
) -> List[Dict[str, Any]]:
    """Shape inventory for the Models page (table + detail modal).

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
        rows.append({
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
            "provider": _oneline(m.get("provider")) if reveal_supply else "",
            "weights_distribution": (
                _oneline(m.get("weights_distribution")) if reveal_supply else ""
            ),
            "training_data_provenance": (
                _oneline(m.get("training_data_provenance")) if reveal_supply else ""
            ),
            "provenance_record": (
                _oneline(m.get("provenance_record")) if reveal_supply else ""
            ),
        })
    return rows

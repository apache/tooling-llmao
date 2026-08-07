"""Configuration for the llmao control plane.

Settings are loaded from asfquart's ``app.cfg`` (YAML ``config.yaml`` next to
``main.py``). That file is local-only and holds secrets — see
``config.yaml.example``. There is no separate "auth mode": the app is always
asfquart.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional


def _get(mapping: Any, key: str, default: Any = None) -> Any:
    if mapping is None:
        return default
    if isinstance(mapping, Mapping):
        return mapping.get(key, default)
    return getattr(mapping, key, default)


@dataclass
class Settings:
    # --- litellm backend --------------------------------------------------
    # "mock"  -> in-process fake teams/usage (no network; good for demos/CI).
    # "proxy" -> talk to a real litellm proxy admin API (production).
    litellm_mode: str = "mock"
    litellm_base_url: str = "http://localhost:4000"
    litellm_master_key: str = "sk-llmao-master-dev"
    request_timeout_s: int = 30

    # --- Budgets ----------------------------------------------------------
    default_team_budget_usd: float = 100.0
    budget_duration: str = "30d"

    # --- Storage / admins -------------------------------------------------
    state_path: str = "./llmao-state.json"
    site_admins: List[str] = field(default_factory=list)

    @classmethod
    def from_cfg(cls, cfg: Any = None) -> Settings:
        """Build settings from an asfquart EasyDict / plain dict config root."""
        cfg = cfg or {}
        litellm = _get(cfg, "litellm") or {}
        budgets = _get(cfg, "budgets") or {}
        site_admins = _get(cfg, "site_admins") or []
        if isinstance(site_admins, str):
            site_admins = [s.strip() for s in site_admins.split(",") if s.strip()]

        mode = str(_get(litellm, "mode", "mock") or "mock").strip().lower()
        return cls(
            litellm_mode=mode,
            litellm_base_url=str(_get(litellm, "base_url", "http://localhost:4000")),
            litellm_master_key=str(_get(litellm, "master_key", "sk-llmao-master-dev")),
            request_timeout_s=int(_get(litellm, "request_timeout_s", 30)),
            default_team_budget_usd=float(_get(budgets, "default_team_budget_usd", 100)),
            budget_duration=str(_get(budgets, "duration", "30d")),
            state_path=str(_get(cfg, "state_path", "./llmao-state.json")),
            site_admins=list(site_admins),
        )

    @property
    def is_mock_llm(self) -> bool:
        return self.litellm_mode != "proxy"

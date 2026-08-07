"""Configuration for the llmao control plane.

Everything is environment-driven so the same image runs locally, in CI, and
in production. Defaults are chosen so that `python -m llmao.app` works on a
laptop with no external services: dev-stub auth and a mock litellm backend.

Set LLMAO_AUTH_MODE=asf and LLMAO_LITELLM_MODE=proxy in production.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: List[str]) -> List[str]:
    raw = os.getenv(name)
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    # --- Auth -------------------------------------------------------------
    # "dev" -> dev-stub login (no external calls; pick a uid + projects).
    # "asf" -> real asfquart OAuth (oauth.apache.org) + LDAP.
    auth_mode: str = field(default_factory=lambda: os.getenv("LLMAO_AUTH_MODE", "dev"))

    # Site admins (uids) always allowed, may view all projects' activity.
    site_admins: List[str] = field(default_factory=lambda: _list("LLMAO_SITE_ADMINS", []))

    # Secret for signing the asfquart/quart session cookie.
    app_secret: str = field(default_factory=lambda: os.getenv("LLMAO_APP_SECRET", "dev-insecure-secret-change-me"))

    # --- litellm backend --------------------------------------------------
    # "mock"  -> in-process fake teams/usage (no network; good for demos/CI).
    # "proxy" -> talk to a real litellm proxy admin API (production).
    litellm_mode: str = field(default_factory=lambda: os.getenv("LLMAO_LITELLM_MODE", "mock"))

    # Base URL of the litellm proxy (when litellm_mode == "proxy").
    litellm_base_url: str = field(default_factory=lambda: os.getenv("LLMAO_LITELLM_BASE_URL", "http://localhost:4000"))

    # The litellm proxy *master* key, used by the seam to provision teams and
    # mint keys via the proxy's /team and /key admin endpoints.
    litellm_master_key: str = field(default_factory=lambda: os.getenv("LLMAO_LITELLM_MASTER_KEY", "sk-llmao-master-dev"))

    # How long (seconds) to wait for litellm *admin* HTTP calls.
    request_timeout_s: int = field(default_factory=lambda: int(os.getenv("LLMAO_REQUEST_TIMEOUT_S", "30")))

    # --- Budgets ----------------------------------------------------------
    # Default monthly budget (USD) granted to a PMC team on first provision.
    default_team_budget_usd: float = field(default_factory=lambda: float(os.getenv("LLMAO_DEFAULT_TEAM_BUDGET_USD", "100")))
    budget_duration: str = field(default_factory=lambda: os.getenv("LLMAO_BUDGET_DURATION", "30d"))

    # --- Storage ----------------------------------------------------------
    # Where the seam persists its ASF-project -> litellm-team mapping and the
    # mock backend keeps usage. A JSON file keeps Phase 1 dependency-free;
    # swap for a real DB later without touching callers.
    state_path: str = field(default_factory=lambda: os.getenv("LLMAO_STATE_PATH", "./llmao-state.json"))

    host: str = field(default_factory=lambda: os.getenv("LLMAO_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("LLMAO_PORT", "8080")))

    @property
    def is_dev_auth(self) -> bool:
        return self.auth_mode != "asf"

    @property
    def is_mock_llm(self) -> bool:
        return self.litellm_mode != "proxy"


settings = Settings()
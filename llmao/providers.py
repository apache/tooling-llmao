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

"""Model-API provider accounts: config.yaml ``providers``.

A provider is an account we pay to serve models: xAI, OpenRouter. Each one
lists the models it serves for us, and each line is one deployment::

    providers:
      openrouter:
        api_key: sk-or-...
        api_base: https://openrouter.ai/api/v1
        models:
          grok-4: openrouter/x-ai/grok-4      # model_name: the provider's model, as LiteLLM names it

Where a model is served is a fact about the deployment, never the model, so
none of this is in models.yaml. The same model can be listed under several
providers and run on our own boxes as well; which one serves a request is
LiteLLM's routing, steered by ``order`` (fleet.ORDER_SELF_HOSTED and
fleet.ORDER_SPEND).

The key lives here, in config.yaml, which is not committed. A developer
without a hosting account can list a personal one and nothing shared changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


@dataclass(frozen=True)
class ProviderDeployment:
    """One model served through one provider account."""

    provider: str
    model_name: str
    # The provider's own model, in LiteLLM's form: "xai/grok-4".
    litellm_model: str
    api_base: str


def provider_api_key(cfg: Any, name: str) -> str:
    """config.yaml providers.<name>.api_key, or "" if it is not there."""
    entry = (cfg.get("providers") or {}).get(name) or {}
    return str(entry.get("api_key") or "").strip()


def provider_deployments(cfg: Any) -> list[ProviderDeployment]:
    """Every deployment listed under config.yaml providers. Assumes validate_providers ran."""
    out = []
    for name, entry in (cfg.get("providers") or {}).items():
        api_base = str(entry.api_base).strip()
        for model_name, litellm_model in entry.models.items():
            out.append(ProviderDeployment(str(name), str(model_name), str(litellm_model).strip(), api_base))
    return out


def validate_providers(cfg: Any, models: list) -> None:
    """Fail-fast if config.yaml providers is malformed or names an unknown model.

    The isinstance checks are for what a hand-written config.yaml can hold
    where a mapping belongs: a string, a list, or nothing at all.
    """
    providers = cfg.get("providers")
    if providers is None:
        return
    if not isinstance(providers, dict):
        raise ValueError("config.yaml: providers must be a mapping of provider name to account")
    known = {str(model.model_name) for model in models}
    for name, entry in providers.items():
        where = f"config.yaml: providers.{name}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be a mapping with api_key, api_base and models")
        key = str(entry.get("api_key") or "").strip()
        if not key or key.startswith("CHANGE_ME"):
            raise ValueError(f"{where}.api_key is required")
        base = urlparse(str(entry.get("api_base") or "").strip())
        if base.scheme not in ("http", "https") or not base.netloc:
            raise ValueError(f"{where}.api_base must be an http(s) URL")
        served = entry.get("models")
        if not isinstance(served, dict):
            raise ValueError(f"{where}.models must be a mapping of model_name to the provider's model")
        for model_name, litellm_model in served.items():
            if str(model_name) not in known:
                raise ValueError(f"{where}.models: {model_name} is not in models.yaml")
            if not str(litellm_model or "").strip():
                raise ValueError(f"{where}.models.{model_name} needs the provider's model, e.g. xai/grok-4")

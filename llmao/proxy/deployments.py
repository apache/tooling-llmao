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
"""LiteLLM's deployments of the fleet: adding, removing, noticing disagreement.

A deployment is one place a model is served from. LiteLLM sends requests to
the ones it has been told about, so each has to be registered there when it
comes up and removed when it goes down, and the two lists compared now and
then in case they have drifted.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlparse

from llmao.fleet import (
    LEGACY_SERVERS,
    ORDER_SELF_HOSTED,
    ORDER_SPEND,
    ExtraLiteLLM,
    normalize_peer_ip,
    snapshot_for_status,
)
from llmao.model_status import (
    HEALTHY,
    SKEW_IN_LITELLM_NOT_IN_CONFIG,
    SKEW_PHRASE,
    STALLED,
    UNHEALTHY,
    DeploymentSnapshot,
    deployment_status,
)
from llmao.providers import provider_api_key
from llmao.proxy.errors import BackendUnavailableError

_LOGGER = logging.getLogger(__name__)


class DeploymentManager:
    """Keeps LiteLLM's deployments in step with the fleet.

    Handed the transport to LiteLLM and the fleet. It holds no state of its
    own: what it learns is written onto the fleet's FleetDeployment records.
    """

    def __init__(self, cfg: Any, fleet: Any, transport: Any):
        self._cfg = cfg
        self.fleet = fleet
        self._transport = transport

    def deployment_body(self, dep, *, api_key: str | None = None) -> dict:
        """POST /model/new payload for one deployment.

        Our boxes: models.yaml litellm_params as the template, plus this
        server's api_base and key. A provider: all of it from config.yaml
        providers, since models.yaml says nothing about who serves a model.

        `api_key` overrides the deployment's own. Callers that can probe the
        box pass what it actually accepts; see _working_api_key.
        """
        entry = self.fleet.models.get(dep.model_name)
        if entry is None:
            raise BackendUnavailableError(f"models.yaml missing {dep.model_name}; cannot POST /model/new")
        if not dep.api_base:
            raise BackendUnavailableError(f"{dep.name}: no api_base for /model/new")
        if dep.provider is not None:
            params = {
                "model": dep.litellm_model,
                "api_base": dep.api_base,
                "api_key": provider_api_key(self._cfg, dep.provider),
                "order": ORDER_SPEND,
            }
        else:
            params = dict(entry.litellm_params)
            params["api_base"] = dep.api_base
            if dep.self_hosted and dep.vllm is not None and dep.vllm.api_key:
                params["api_key"] = api_key or dep.vllm.api_key
            params["order"] = ORDER_SELF_HOSTED

        info: dict[str, Any] = {
            "self_hosted": dep.self_hosted,
            "asf_api_base": _norm_base(dep.api_base),
        }
        if dep.access_groups:
            # What LiteLLM reads to decide which teams may use this
            # deployment. The pretend projects are limited to the
            # self-hosted group, so a key with no project can reach our own
            # boxes and nothing that costs money.
            info["access_groups"] = sorted(dep.access_groups)

        # Price self-hosted tokens so usage reporting shows something other
        # than 0.00. LiteLLM has no cost-map entry for these models, so every
        # completion prices at nothing and a project's draw is invisible.
        #
        # The price is derived per model in models.yaml from measured
        # throughput: hourly rental over tokens per second, input and output
        # separately because prefill runs at thousands per second and decode
        # at tens. On this fleet that is a 12-27x ratio, close to what
        # commercial providers charge.
        #
        # It follows the card rather than being one fleet-wide figure, because
        # a faster box costs more per hour and should cost more per token --
        # otherwise a model's price stops tracking what serving it costs.
        #
        # IT IS NOT A MARGINAL COST. The boxes are rented continuously, so a
        # token costs the same whether or not anyone asks for it, and the
        # figure above assumes a box decoding flat out. At 25% utilisation the
        # real cost is four times this; at 5%, twenty times. Read it as a unit
        # of fair share that happens to carry a dollar sign -- useful for
        # spotting a runaway job and comparing one project's draw against
        # another's, and wrong the moment anyone treats it as money the
        # Foundation could have kept.
        #
        # The UI shows it alongside a token count so the distinction survives
        # contact with a reader.
        if dep.self_hosted:
            pricing = getattr(entry, "pricing", None)
            if pricing is not None:
                for name in ("input_cost_per_token", "output_cost_per_token"):
                    val = getattr(pricing, name, None)
                    if val is not None:
                        info[name] = float(val)
                # Marks the row as capacity rather than money, so a report can
                # keep the two apart instead of adding them together.
                info["cost_basis"] = dep.cost_basis

        return {
            "model_name": dep.model_name,
            "litellm_params": params,
            "model_info": info,
        }

    async def _model_info_rows(self) -> list:
        resp = await self._transport.request("GET", "model/info")
        self._transport.raise_http(resp)
        body = resp.json()
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            return body.get("data") or body.get("models") or []
        return []

    def _litellm_id(self, row: dict, dep) -> str | None:
        if str(row.get("model_name") or "") != dep.model_name:
            return None
        want = _norm_base(dep.api_base)
        info = row.get("model_info") or {}
        if not isinstance(info, dict):
            info = {}
        if (
            _norm_base(info.get("asf_api_base")) == want
            or _norm_base(
                (row.get("litellm_params") or {}).get("api_base") if isinstance(row.get("litellm_params"), dict) else ""
            )
            == want
        ):
            return str(info.get("id") or row.get("model_id") or "") or None
        return None

    async def _working_api_key(self, dep) -> str | None:
        """The bearer to register for this server.

        LEGACY_SERVERS were launched with the shared selfhost_api_key.
        validate_fleet already required that key when one of those hosts is
        configured, so this just returns it. Every other box uses the key
        derived from the salt. No probe: trying the wrong one 401s and the
        next pass deletes the route.
        """
        if not dep.self_hosted or dep.vllm is None:
            return None
        srv = dep.vllm
        if (normalize_peer_ip(srv.host), int(srv.listen_port)) in LEGACY_SERVERS:
            return str(self._cfg.fleet.selfhost_api_key).strip()
        return srv.api_key or None

    async def _deployment_present(self, dep) -> bool:
        routes = _routes_from_model_info(await self._model_info_rows())
        if dep.provider is not None:
            # Registered at every startup, so a restart must find the last one
            # rather than add a second copy.
            want = (_norm_base(dep.api_base), dep.model_name)
            return any((norm, model_name) == want for norm, _host, _port, model_name in routes)
        if not dep.self_hosted or dep.vllm is None:
            return False
        return any(dep.vllm.same_deployment(host, port, model_name) for _norm, host, port, model_name in routes)

    async def add_deployment(self, dep) -> None:
        """POST /model/new for this deployment. No serving check.

        Callers decide the gate. sync_selfhost waits until vLLM is SERVING.
        POST /do-add-deployment uses add_refusal for the same gate, then calls
        this. A deployment already present for this server, on the listen port
        or the public port, is not posted again. A 400 or 409 means the same thing.
        """
        if await self._deployment_present(dep):
            dep.in_litellm = True
            _LOGGER.info("model/new already present %s@%s", dep.name, dep.api_base)
            return
        body = self.deployment_body(dep, api_key=await self._working_api_key(dep))
        resp = await self._transport.request("POST", "model/new", json=body)
        if resp.status_code in (400, 409):
            dep.in_litellm = True
            _LOGGER.info("model/new already present %s@%s", dep.name, dep.api_base)
            return
        self._transport.raise_http(resp)
        dep.in_litellm = True
        _LOGGER.info("model/new %s@%s", dep.name, dep.api_base)

    async def delete_deployment(self, dep) -> None:
        rows = await self._model_info_rows()
        found = None
        for row in rows:
            if isinstance(row, dict):
                found = self._litellm_id(row, dep)
                if found:
                    break
        if not found:
            dep.in_litellm = False
            _LOGGER.warning("model/delete: no LiteLLM id for %s@%s", dep.name, dep.api_base)
            return
        resp = await self._transport.request("POST", "model/delete", json={"id": found})
        self._transport.raise_http(resp)
        dep.in_litellm = False
        _LOGGER.info("model/delete %s@%s id=%s", dep.name, dep.api_base, found)

    async def ensure_providers(self) -> None:
        """At startup: POST /model/new for each config.yaml providers deployment."""
        for dep in self.fleet.deployments:
            if dep.provider is None or not dep.api_base:
                continue
            if dep.in_litellm:
                continue
            await self.add_deployment(dep)

    async def sync_selfhost(self) -> None:
        """After vLLM probes: register serving backends, drop down ones."""
        for dep in self.fleet.deployments:
            if not dep.self_hosted or dep.vllm is None:
                continue
            srv = dep.vllm
            if srv.state == HEALTHY and dep.api_base and not dep.in_litellm:
                await self.add_deployment(dep)
            elif srv.state in (UNHEALTHY, STALLED) and dep.in_litellm:
                await self.delete_deployment(dep)

    async def check_config_skew(self) -> None:
        resp = await self._transport.request("GET", "model/info")
        self._transport.raise_http(resp)
        routes = _routes_from_model_info(resp.json())
        by_base: dict[str, list[str]] = {}
        for norm, _host, _port, model_name in routes:
            by_base.setdefault(norm, []).append(model_name)
        pairs = {(norm, model_name) for norm, _host, _port, model_name in routes}
        # One of our boxes serves one model, so its api_base identifies it. A
        # provider's api_base is shared by every model on the account, so a
        # provider deployment is its api_base and model name together.
        ours = {_norm_base(d.api_base) for d in self.fleet.deployments if d.api_base and d.provider is None}
        bought = {(_norm_base(d.api_base), d.model_name) for d in self.fleet.deployments if d.provider is not None}
        now = time.time()
        for dep in self.fleet.deployments:
            base = _norm_base(dep.api_base) if dep.api_base else None
            if dep.provider is not None:
                dep.in_litellm = (base, dep.model_name) in pairs
                dep.config_mismatch = False
                self._store_skew(dep, now)
                continue
            names = by_base.get(base, []) if base else []
            present = any(
                dep.vllm is not None and dep.vllm.same_deployment(host, port, model_name)
                for _norm, host, port, model_name in routes
            )
            dep.in_litellm = bool(names) or present
            dep.config_mismatch = any(name != dep.model_name for name in names)
            self._store_skew(dep, now)
        extra: list[ExtraLiteLLM] = []
        for norm, host, port, model_name in routes:
            if norm in ours or (norm, model_name) in bought:
                continue
            view = deployment_status(
                DeploymentSnapshot(model_name=model_name, self_hosted=False, litellm_only=True),
                self._cfg.fleet,
                now,
            )
            extra.append(ExtraLiteLLM(host=host, port=port, model_name=model_name, skew=view.skew))
        self.fleet.extra_litellm = extra
        if extra:
            _LOGGER.warning(
                "skew: %s %s",
                SKEW_PHRASE[SKEW_IN_LITELLM_NOT_IN_CONFIG],
                [(row.host, row.port, row.model_name) for row in extra],
            )

    async def check_health_skew(self) -> None:
        resp = await self._transport.request("GET", "health")
        self._transport.raise_http(resp)
        body = resp.json() if resp.content else {}
        # Each endpoint as (api_base, LiteLLM model). Our boxes match on the
        # base alone; a provider's base is shared, so it matches on both.
        healthy = {
            (_norm_base(x.get("api_base")), x.get("model"))
            for x in (body.get("healthy_endpoints") or [])
            if isinstance(x, dict) and x.get("api_base")
        }
        unhealthy = {
            (_norm_base(x.get("api_base")), x.get("model"))
            for x in (body.get("unhealthy_endpoints") or [])
            if isinstance(x, dict) and x.get("api_base")
        }
        healthy_bases = {base for base, _model in healthy}
        unhealthy_bases = {base for base, _model in unhealthy}
        stamp = time.time()
        for dep in self.fleet.deployments:
            base = _norm_base(dep.api_base) if dep.api_base else None
            if dep.provider is not None:
                litellm_up = (base, dep.litellm_model) in healthy
                litellm_down = (base, dep.litellm_model) in unhealthy
            else:
                litellm_up = bool(base) and base in healthy_bases
                litellm_down = bool(base) and base in unhealthy_bases
            if litellm_up:
                dep.litellm_healthy = True
                dep.litellm_health_at = stamp
            elif litellm_down:
                dep.litellm_healthy = False
                dep.litellm_health_at = stamp
            else:
                dep.litellm_healthy = None
            self._store_skew(dep, stamp)

    def _store_skew(self, dep, now: float) -> None:
        """Replace stored notes with the badges model_status computes now."""
        view = deployment_status(snapshot_for_status(dep, self._cfg.fleet, now), self._cfg.fleet, now)
        fresh = [badge for badge in view.skew if badge not in dep.skew]
        dep.skew = list(view.skew)
        for badge in fresh:
            _LOGGER.warning("skew: %s@%s %s", dep.name, dep.api_base, SKEW_PHRASE[badge])


def _norm_base(url: Any) -> str:
    """Strip trailing slashes and a trailing /v1.

    The identity the skew check compares on: the same route appears with the
    suffix (VllmServer.api_base, and the litellm_params.api_base copied from
    it) and without (asf_api_base, routes pushed before the suffix). The
    comparison must match across both forms.

    (The suffix was once actively wrong: an older LiteLLM appended
    /v1/chat/completions to api_base, so a /v1-suffixed base resolved to
    /v1/v1/..., 404'd at the origin, and surfaced as an OpenAI
    authentication error naming platform.openai.com -- which sends the reader
    after their own key rather than the route. The current LiteLLM appends
    only /chat/completions, so the suffix is what vLLM needs now.)
    """
    s = str(url or "").rstrip("/")
    if s.endswith("/v1"):
        s = s[:-3].rstrip("/")
    return s


def _routes_from_model_info(body: Any) -> list[tuple[str, str, int | None, str]]:
    """Each LiteLLM row as (normalised api_base, host, port, model_name).

    Host and port stay separate. One machine serves several ports, and one
    host:port can still report more than one model name.
    """
    rows = []
    if isinstance(body, list):
        rows = body
    elif isinstance(body, dict):
        rows = body.get("data") or body.get("models") or []
    out: list[tuple[str, str, int | None, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        params = row.get("litellm_params") or {}
        if not isinstance(params, dict) or not params.get("api_base"):
            continue
        norm = _norm_base(params["api_base"])
        parsed = urlparse(norm)
        out.append((norm, parsed.hostname or "", parsed.port, str(row.get("model_name") or "")))
    return out

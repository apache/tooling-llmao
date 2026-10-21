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
    ORPHAN_PASSES,
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
from llmao.reconcile import norm_base

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
        # (api_base, model_name) -> consecutive skew passes seen as an extra.
        # A route absent from one pass is dropped from the dict, so a flapping
        # one never accumulates. See _prune_extras.
        self._extra_passes: dict[tuple[str, str], int] = {}

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
            "asf_api_base": norm_base(dep.api_base),
        }
        if dep.access_groups:
            # What LiteLLM reads to decide which teams may use this
            # deployment. The pretend projects are limited to the
            # self-hosted group, so a key with no project can reach our own
            # boxes and nothing that costs money.
            info["access_groups"] = sorted(dep.access_groups)

        # No price. LiteLLM has no cost-map entry for our own models, so a
        # self-hosted request is recorded at $0.00, and that is kept on
        # purpose: a price here would count against team budgets, and
        # self-hosted use must never block. What a box really costs is its
        # rental, which the capacity report sets against the tokens it served.

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
        want = norm_base(dep.api_base)
        info = row.get("model_info") or {}
        if not isinstance(info, dict):
            info = {}
        if (
            norm_base(info.get("asf_api_base")) == want
            or norm_base(
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
            want = (norm_base(dep.api_base), dep.model_name)
            return any((norm, model_name) == want for norm, _host, _port, model_name, _id in routes)
        if not dep.self_hosted or dep.vllm is None:
            return False
        return any(dep.vllm.same_deployment(host, port, model_name) for _norm, host, port, model_name, _id in routes)

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

    async def _each(self, action, dep) -> None:
        """Run `action` on one deployment; a failure is logged and goes no further.

        The callers walk every deployment. Unguarded, the first one to raise
        ended the walk, and every deployment after it was never registered:
        vLLM up and Healthy, nothing in LiteLLM, the same on every pass.
        """
        try:
            await action(dep)
        except Exception as e:
            _LOGGER.exception("%s failed for %s@%s", action.__name__, dep.name, dep.api_base)
            # Kept for /models, which otherwise shows only that it is missing.
            dep.litellm_error = str(e) or type(e).__name__
            dep.litellm_error_at = time.time()
        else:
            dep.litellm_error = ""
            dep.litellm_error_at = None

    async def ensure_providers(self) -> None:
        """At startup: POST /model/new for each config.yaml providers deployment."""
        for dep in self.fleet.deployments:
            if dep.provider is None or not dep.api_base:
                continue
            if dep.in_litellm:
                continue
            await self._each(self.add_deployment, dep)

    async def sync_selfhost(self) -> None:
        """After vLLM probes: register serving backends, drop down ones.

        Down is Unhealthy or Stalled. A box no provider lists any more counts
        too -- the deployment points at nothing -- but ONLY while it is not
        serving, which is the gate remove_refusal states and the Fleet Remove
        button has always honoured: a box that answers keeps its route.

        e1567cb dropped that condition, and a RunPod pod absent from the pod
        list while it provisioned then had its route deleted and re-added on
        alternating passes for as long as the gap lasted. The box was serving
        throughout. Hysteresis on `unlisted` is the other half of that fix;
        this is the half that says a serving box is never dropped, whatever
        a provider's listing claims.
        """
        for dep in self.fleet.deployments:
            if not dep.self_hosted or dep.vllm is None:
                continue
            srv = dep.vllm
            if srv.state == HEALTHY and dep.api_base and not dep.in_litellm:
                await self._each(self.add_deployment, dep)
            elif dep.in_litellm and (srv.state in (UNHEALTHY, STALLED) or (srv.unlisted and srv.state != HEALTHY)):
                await self._each(self.delete_deployment, dep)

    async def check_config_skew(self) -> None:
        resp = await self._transport.request("GET", "model/info")
        self._transport.raise_http(resp)
        routes = _routes_from_model_info(resp.json())
        by_base: dict[str, list[str]] = {}
        for norm, _host, _port, model_name, _id in routes:
            by_base.setdefault(norm, []).append(model_name)
        pairs = {(norm, model_name) for norm, _host, _port, model_name, _id in routes}
        # One of our boxes serves one model, so its api_base identifies it. A
        # provider's api_base is shared by every model on the account, so a
        # provider deployment is its api_base and model name together.
        ours = {norm_base(d.api_base) for d in self.fleet.deployments if d.api_base and d.provider is None}
        bought = {(norm_base(d.api_base), d.model_name) for d in self.fleet.deployments if d.provider is not None}
        # Servers whose address is not known this pass: no public port yet, or
        # the provider's lookup failed. A LiteLLM deployment on the same host
        # and model may well be theirs, so it is not called an extra.
        unplaced = {
            (normalize_peer_ip(d.vllm.host), d.model_name)
            for d in self.fleet.deployments
            if d.provider is None and d.vllm is not None and not d.api_base
        }
        servers = [d.vllm for d in self.fleet.deployments if d.provider is None and d.vllm is not None]
        now = time.time()
        for dep in self.fleet.deployments:
            base = norm_base(dep.api_base) if dep.api_base else None
            if dep.provider is not None:
                dep.in_litellm = (base, dep.model_name) in pairs
                dep.config_mismatch = False
                self._store_skew(dep, now)
                continue
            if base is None:
                # No address is not evidence that LiteLLM lacks it: the last
                # answer stands. Not even a deployment on the listen port says
                # otherwise -- off-box that port is unreachable, so it is not the
                # deployment this server needs.
                self._store_skew(dep, now)
                continue
            present = any(
                dep.vllm is not None and dep.vllm.same_deployment(host, port, model_name)
                for _norm, host, port, model_name, _id in routes
            )
            names = by_base.get(base, [])
            dep.in_litellm = bool(names) or present
            dep.config_mismatch = any(name != dep.model_name for name in names)
            dep.at_listen_port = any(
                dep.vllm.on_listen_port(host, port, model_name) for _norm, host, port, model_name, _id in routes
            )
            self._store_skew(dep, now)
        extra: list[ExtraLiteLLM] = []
        for norm, host, port, model_name, route_id in routes:
            if norm in ours or (norm, model_name) in bought:
                continue
            if (normalize_peer_ip(host), model_name) in unplaced:
                continue
            # Reported on its server, as "in LiteLLM at the listen port".
            if any(srv.on_listen_port(host, port, model_name) for srv in servers):
                continue
            view = deployment_status(
                DeploymentSnapshot(model_name=model_name, self_hosted=False, litellm_only=True),
                self._cfg.fleet,
                now,
            )
            seen = self._extra_passes.get((norm, model_name), 0) + 1
            extra.append(
                ExtraLiteLLM(
                    host=host,
                    port=port,
                    model_name=model_name,
                    skew=view.skew,
                    route_id=route_id,
                    api_base=norm,
                    passes=seen,
                )
            )
        self.fleet.extra_litellm = extra
        self._extra_passes = {(row.api_base, row.model_name): row.passes for row in extra}
        if extra:
            _LOGGER.warning(
                "skew: %s %s",
                SKEW_PHRASE[SKEW_IN_LITELLM_NOT_IN_CONFIG],
                [(row.host, row.port, row.model_name, row.passes) for row in extra],
            )
        await self._prune_extras(extra)

    async def _prune_extras(self, extra: list[ExtraLiteLLM]) -> None:
        """Delete a route no deployment claims, once it has persisted.

        This is the deregistration hole in tooling-llmao#29: routes are created
        on the transition into SERVING and deleted on the transition into DOWN,
        and a server removed from fleet.hosts makes neither transition -- so its
        route outlives it, and the model is unreachable through the proxy while
        /fleet shows it serving. check_config_skew has always computed exactly
        this set; it only reported it.

        Why a threshold and not the first sighting. An extra is inferred from
        absence, and absence has innocent causes: a provider lookup that failed
        this pass, a box mid-recreate, a route llmao itself is about to claim.
        The guards above remove the ones that can be named (`unplaced` for a
        server whose address is not known yet), but not the ones that cannot,
        so ORPHAN_PASSES consecutive skew passes are required -- its own count
        on its own clock, for the same reasoning as UNLISTED_PASSES on the
        provider listings.

        No probe gate. An orphan's credential is unknown -- derivation needs the
        LISTEN port and a route only carries the public one -- and the one
        endpoint needing no credential is vLLM's /health, which cannot see the
        served model name. A /health gate would therefore spare precisely the
        routes that 404 on real traffic, which are the ones worth deleting.

        Deleting is also what makes a restart converge: the counter starts
        empty, so a portal that comes up to a proxy full of stale routes clears
        them after ORPHAN_PASSES skew passes rather than waiting for a
        transition that already happened.
        """
        for row in extra:
            if row.passes < ORPHAN_PASSES:
                continue
            if not row.route_id:
                _LOGGER.warning(
                    "skew: %s:%s %s has no LiteLLM id; cannot remove it",
                    row.host,
                    row.port,
                    row.model_name,
                )
                continue
            resp = await self._transport.request("POST", "model/delete", json={"id": row.route_id})
            if resp.status_code >= 400:
                _LOGGER.warning(
                    "model/delete orphan %s@%s id=%s failed: %s",
                    row.model_name,
                    row.api_base,
                    row.route_id,
                    resp.status_code,
                )
                continue
            _LOGGER.info(
                "model/delete orphan %s@%s id=%s after %d passes with no deployment",
                row.model_name,
                row.api_base,
                row.route_id,
                row.passes,
            )
            self._extra_passes.pop((row.api_base, row.model_name), None)

    async def check_health_skew(self) -> None:
        resp = await self._transport.request("GET", "health")
        self._transport.raise_http(resp)
        body = resp.json() if resp.content else {}
        # Each endpoint as (api_base, LiteLLM model). Our boxes match on the
        # base alone; a provider's base is shared, so it matches on both.
        healthy = {
            (norm_base(x.get("api_base")), x.get("model"))
            for x in (body.get("healthy_endpoints") or [])
            if isinstance(x, dict) and x.get("api_base")
        }
        unhealthy = {
            (norm_base(x.get("api_base")), x.get("model"))
            for x in (body.get("unhealthy_endpoints") or [])
            if isinstance(x, dict) and x.get("api_base")
        }
        healthy_bases = {base for base, _model in healthy}
        unhealthy_bases = {base for base, _model in unhealthy}
        stamp = time.time()
        for dep in self.fleet.deployments:
            base = norm_base(dep.api_base) if dep.api_base else None
            if base is None:
                # Nothing to match LiteLLM's answer to. The last result stands
                # rather than being wiped for the four hours until the next check.
                continue
            if dep.provider is not None:
                litellm_up = (base, dep.litellm_model) in healthy
                litellm_down = (base, dep.litellm_model) in unhealthy
            else:
                litellm_up = base in healthy_bases
                litellm_down = base in unhealthy_bases
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


def _routes_from_model_info(body: Any) -> list[tuple[str, str, int | None, str, str]]:
    """Each LiteLLM row as (normalised api_base, host, port, model_name, id).

    Host and port stay separate. One machine serves several ports, and one
    host:port can still report more than one model name. The id is what
    /model/delete takes, so a route with no deployment behind it can be
    removed without a second lookup.
    """
    rows = []
    if isinstance(body, list):
        rows = body
    elif isinstance(body, dict):
        rows = body.get("data") or body.get("models") or []
    out: list[tuple[str, str, int | None, str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        params = row.get("litellm_params") or {}
        if not isinstance(params, dict) or not params.get("api_base"):
            continue
        norm = norm_base(params["api_base"])
        parsed = urlparse(norm)
        info = row.get("model_info") or {}
        route_id = str(info.get("id") or "") if isinstance(info, dict) else ""
        out.append((norm, parsed.hostname or "", parsed.port, str(row.get("model_name") or ""), route_id))
    return out

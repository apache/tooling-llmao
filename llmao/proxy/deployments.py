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
    ORDER_SELF_HOSTED,
    ORDER_SPEND,
    ORPHAN_PASSES,
    ExtraLiteLLM,
    normalize_peer_ip,
    snapshot_for_status,
)
from llmao.intent import provider_api_key
from llmao.model_status import (
    FOREIGN,
    HEALTHY,
    REJECTED,
    SKEW_IN_LITELLM_NOT_IN_CONFIG,
    SKEW_PHRASE,
    STALLED,
    UNHEALTHY,
    deployment_status,
)
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
        # (api_base, model_name) -> consecutive skew passes seen as an orphan.
        # A route absent from one pass is dropped from the dict, so a flapping
        # one never reaches ORPHAN_PASSES and the attention strip.
        self._extra_passes: dict[tuple[str, str], int] = {}

    def deployment_body(self, dep) -> dict:
        """POST /model/new payload for one deployment.

        Our boxes: models.yaml litellm_params as the template, plus this
        server's api_base and the key its intent was minted with. A provider:
        all of it from config.yaml providers, since models.yaml says nothing
        about who serves a model.
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
                params["api_key"] = dep.vllm.api_key
            params["order"] = ORDER_SELF_HOSTED

        info: dict[str, Any] = {
            "self_hosted": dep.self_hosted,
            # The dialable base this deployment was registered at, normalised.
            "asf_api_base": norm_base(dep.api_base),
            # The stamp: the deployment's key, so a LiteLLM row says which of
            # ours it is without translating its public port back to the
            # listen port. Built from the LISTEN port, so a remap keeps it.
            # The row's model_name is the other half of the key.
            "llmao_key": dep.key.url,
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
        return _rows_of(resp.json())

    def _litellm_id(self, row: dict, dep) -> str | None:
        """This deployment's LiteLLM id from one /model/info row, or None when the row is not it.

        The stamp decides: a row stamped with this key is this deployment at
        whatever address it was registered, including a public port that has
        since been remapped. Every row llmao adds is stamped, and the rows
        from before stamping were stamped in place on 2026-10-22, so a row
        with no stamp is not one of ours: an orphan, never this deployment.
        """
        if str(row.get("model_name") or "") != dep.model_name:
            return None
        info = row.get("model_info") or {}
        if not isinstance(info, dict):
            info = {}
        stamp = info.get("llmao_key")
        if not stamp or norm_base(stamp) != dep.key.url:
            return None
        return str(info.get("id") or row.get("model_id") or "") or None

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

    async def add_deployment(self, dep) -> bool:
        """POST /model/new for this deployment. No serving check. True when a write was sent.

        Callers decide the gate. sync_selfhost waits until vLLM is SERVING.
        POST /do-add-deployment uses add_refusal for the same gate, then calls
        this. A deployment already present for this server, on the listen port
        or the public port, is not posted again. A 400 or 409 means the same thing.
        """
        if await self._deployment_present(dep):
            dep.in_litellm = True
            _LOGGER.info("model/new already present %s@%s", dep.name, dep.key.display)
            return False
        body = self.deployment_body(dep)
        resp = await self._transport.request("POST", "model/new", json=body)
        if resp.status_code in (400, 409):
            dep.in_litellm = True
            _LOGGER.info("model/new already present %s@%s", dep.name, dep.key.display)
            return True
        self._transport.raise_http(resp)
        dep.in_litellm = True
        _LOGGER.info("model/new %s@%s", dep.name, dep.key.display)
        return True

    async def delete_deployment(self, dep) -> bool:
        """POST /model/delete for this deployment's LiteLLM id. True when a write was sent."""
        rows = await self._model_info_rows()
        found = None
        for row in rows:
            if isinstance(row, dict):
                found = self._litellm_id(row, dep)
                if found:
                    break
        if not found:
            dep.in_litellm = False
            _LOGGER.warning("model/delete: no LiteLLM id for %s@%s", dep.name, dep.key.display)
            return False
        resp = await self._transport.request("POST", "model/delete", json={"id": found})
        self._transport.raise_http(resp)
        dep.in_litellm = False
        _LOGGER.info("model/delete %s@%s id=%s", dep.name, dep.key.display, found)
        return True

    async def _each(self, action, dep) -> bool:
        """Run `action` on one deployment; a failure is logged and goes no further.

        The callers walk every deployment. Unguarded, the first one to raise
        ended the walk, and every deployment after it was never registered:
        vLLM up and Healthy, nothing in LiteLLM, the same on every pass.
        Returns whether the action sent a write to LiteLLM.
        """
        try:
            wrote = await action(dep)
        except Exception as e:
            _LOGGER.exception("%s failed for %s@%s", action.__name__, dep.name, dep.key.display)
            # Kept for /models, which otherwise shows only that it is missing.
            dep.litellm_error = str(e) or type(e).__name__
            dep.litellm_error_at = time.time()
            return False
        dep.litellm_error = ""
        dep.litellm_error_at = None
        return bool(wrote)

    async def _confirm(self) -> None:
        """Re-read the LiteLLM deployments straight after a write, so presence is observed, not assumed.

        LiteLLM updates its router inside the /model/new and /model/delete
        handlers before answering (1.105.0: add_new_model awaits
        proxy_config.add_deployment; delete_model calls
        llm_router.delete_deployment), so the read that follows sees the
        write. If the read fails, the last answer stands and the next skew
        pass reads again; a failure here never raises out of the probe tick.
        """
        try:
            routes = _routes_from_model_info(await self._model_info_rows())
        except Exception as e:
            _LOGGER.warning("the LiteLLM deployments could not be re-read after a write: %s", e)
            return
        by_base, pairs = _index_deployments(routes)
        for dep in self.fleet.deployments:
            self._mark_presence(dep, routes, by_base, pairs)

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
        wrote = False
        for dep in self.fleet.deployments:
            if not dep.self_hosted or dep.vllm is None:
                continue
            srv = dep.vllm
            if srv.state == HEALTHY and dep.api_base and not dep.in_litellm:
                wrote = await self._each(self.add_deployment, dep) or wrote
            elif dep.in_litellm and (
                srv.state in (UNHEALTHY, STALLED, REJECTED, FOREIGN) or (srv.unlisted and srv.state != HEALTHY)
            ):
                wrote = await self._each(self.delete_deployment, dep) or wrote
        if wrote:
            await self._confirm()

    def _mark_presence(self, dep, routes, by_base, pairs) -> None:
        """Set what one read of the LiteLLM deployments says about this deployment.

        A provider deployment is present when its (base, model name) pair is.
        One of our boxes is present when a deployment is at its public port,
        or at its base under any name (then config_mismatch says which); one
        at its listen port is noted, not counted.
        """
        base = norm_base(dep.api_base) if dep.api_base else None
        if dep.provider is not None:
            dep.in_litellm = (base, dep.model_name) in pairs
            dep.config_mismatch = False
            return
        if base is None:
            # No address is not evidence that LiteLLM lacks it: the last
            # answer stands. Not even a deployment on the listen port says
            # otherwise -- off-box that port is unreachable, so it is not the
            # deployment this server needs.
            return
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

    async def check_config_skew(self) -> None:
        resp = await self._transport.request("GET", "model/info")
        self._transport.raise_http(resp)
        body = resp.json()
        routes = _routes_from_model_info(body)
        stamps = _stamps_from_model_info(body)
        by_base, pairs = _index_deployments(routes)
        # One of our boxes serves one model, so its api_base identifies it. A
        # provider's api_base is shared by every model on the account, so a
        # provider deployment is its api_base and model name together.
        ours = {norm_base(d.api_base) for d in self.fleet.deployments if d.api_base and d.provider is None}
        bought = {(norm_base(d.api_base), d.model_name) for d in self.fleet.deployments if d.provider is not None}
        # Servers whose address is not known this pass: no public port yet, or
        # the provider's lookup failed. A LiteLLM deployment on the same host
        # and model may well be theirs, so it is not called an extra.
        unplaced = {
            (d.vllm.address or "", d.model_name)
            for d in self.fleet.deployments
            if d.provider is None and d.vllm is not None and not d.api_base
        }
        servers = [d.vllm for d in self.fleet.deployments if d.provider is None and d.vllm is not None]
        now = time.time()
        for dep in self.fleet.deployments:
            self._mark_presence(dep, routes, by_base, pairs)
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
            # An orphan: something instructed LiteLLM to serve it, and llmao
            # does not know why, so it is shown and never deleted unattended
            # (an admin stops tracking it, or approves the host again). The
            # pass count is a debounce: absence has innocent causes, so the
            # strip waits ORPHAN_PASSES. The stamp says who added it.
            seen = self._extra_passes.get((norm, model_name), 0) + 1
            row = ExtraLiteLLM(
                host=host,
                port=port,
                model_name=model_name,
                skew=(SKEW_IN_LITELLM_NOT_IN_CONFIG,),
                route_id=route_id,
                api_base=norm,
                passes=seen,
                llmao_key=stamps.get(route_id, ""),
            )
            extra.append(row)
            if seen == ORPHAN_PASSES:
                _LOGGER.warning(
                    "skew: %s %s:%s %s (%s)",
                    SKEW_PHRASE[SKEW_IN_LITELLM_NOT_IN_CONFIG],
                    host,
                    port,
                    model_name,
                    (row.llmao_key and "llmao added it") or "not added by llmao",
                )
        self.fleet.extra_litellm = extra
        self._extra_passes = {(row.api_base, row.model_name): row.passes for row in extra}

    async def delete_extra(self, row: ExtraLiteLLM) -> None:
        """POST /model/delete for an orphan, on an admin's Stop tracking. extra_refusal is the gate."""
        resp = await self._transport.request("POST", "model/delete", json={"id": row.route_id})
        self._transport.raise_http(resp)
        _LOGGER.info("model/delete orphan %s@%s id=%s", row.model_name, row.api_base, row.route_id)
        self._extra_passes.pop((row.api_base, row.model_name), None)
        self.fleet.extra_litellm = [r for r in self.fleet.extra_litellm if r is not row]
        await self._confirm()

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
            _LOGGER.warning("skew: %s@%s %s", dep.name, dep.key.display, SKEW_PHRASE[badge])


def _index_deployments(rows) -> tuple[dict[str, list[str]], set[tuple[str, str]]]:
    """The LiteLLM deployments by normalised base (the model names at each), and as (base, model name) pairs."""
    by_base: dict[str, list[str]] = {}
    for norm, _host, _port, model_name, _id in rows:
        by_base.setdefault(norm, []).append(model_name)
    pairs = {(norm, model_name) for norm, _host, _port, model_name, _id in rows}
    return by_base, pairs


def _rows_of(body: Any) -> list:
    """The deployment rows of a GET /model/info answer, whichever shape LiteLLM gave it."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("data") or body.get("models") or []
    return []


def _routes_from_model_info(body: Any) -> list[tuple[str, str, int | None, str, str]]:
    """Each LiteLLM row as (normalised api_base, host, port, model_name, id).

    Host and port stay separate. One machine serves several ports, and one
    host:port can still report more than one model name. The id is what
    /model/delete takes, so a route with no deployment behind it can be
    removed without a second lookup.
    """
    out: list[tuple[str, str, int | None, str, str]] = []
    for row in _rows_of(body):
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


def _stamps_from_model_info(body: Any) -> dict[str, str]:
    """LiteLLM id -> the ``llmao_key`` stamp, for the rows that carry one (llmao's own adds)."""
    out: dict[str, str] = {}
    for row in _rows_of(body):
        info = isinstance(row, dict) and row.get("model_info")
        if isinstance(info, dict) and info.get("id") and info.get("llmao_key"):
            out[str(info["id"])] = str(info["llmao_key"])
    return out

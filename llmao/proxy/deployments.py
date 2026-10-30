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
"""LiteLLM's side of the fleet: what it has, what it thinks of it, and the writes that change it.

Two sources (docs/fleet-reconcile-design.md): ``LiteLLMDeployment``, one per
``GET /model/info`` entry, and ``LiteLLMHealth``, LiteLLM's own ``GET
/health`` verdict on a deployment. Each is a frozen snapshot of what LiteLLM
said, with nothing of ours on it; the fleet joins them to the intended
deployments (``Fleet.update_litellm_deployments``, ``Fleet.update_litellm_health``).

``DeploymentManager`` is the executor: it fetches the two sources, hands
them to the fleet, and sends ``/model/new`` and ``/model/delete`` for the
keys it is told to add or drop. A fetch only fetches; every place that
folds a fetch into the fleet says so on its own line, and the executor's
own decisions read the fetch, never the fleet's copy of it. Every write
is followed by a fetch that is folded, so presence is observed, not
assumed. This module never imports ``llmao.fleet``: the fleet is handed
in.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from llmao.intent import provider_api_key
from llmao.model_status import FOREIGN, HEALTHY, REJECTED, STALLED, UNHEALTHY
from llmao.proxy.errors import BackendUnavailableError
from llmao.reconcile import DeploymentKey, deployment_key, norm_base

_LOGGER = logging.getLogger(__name__)

# LiteLLM's routing order: among a model name's healthy deployments it uses
# only those with the lowest order. Our boxes come first; a provider we pay
# serves only once none of them can -- llmao has removed them, or LiteLLM has
# put them in cooldown. A busy box still takes the request: busy is not down.
# Every deployment carries one, because LiteLLM drops a deployment with no
# order as soon as any other deployment of the name has one.
ORDER_SELF_HOSTED = 1
ORDER_SPEND = 2


@dataclass(frozen=True)
class LiteLLMDeployment:
    """One deployment as ``GET /model/info`` reports it.

    ``key`` is the stamp llmao writes on its own adds (``model_info.llmao_key``,
    the listen URL) with the row's model name: which of ours this is, at
    whatever address it was registered, a remapped public port included.
    None on a row llmao did not add. ``api_base`` is the dialable base it was
    registered at, normalised. ``id`` is what ``/model/delete`` takes; empty
    when LiteLLM reported the row without one.
    """

    id: str
    model_name: str
    api_base: str
    key: DeploymentKey | None

    @property
    def host(self) -> str:
        return urlparse(self.api_base).hostname or ""

    @property
    def port(self) -> int | None:
        return urlparse(self.api_base).port

    @property
    def display(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class LiteLLMDeployments:
    """One read of the LiteLLM deployments: every one it has, by key, and when it was read.

    The stamp is the only way a row is matched to a deployment of ours.
    Every row llmao adds is stamped, and the rows from before stamping were
    stamped in place on 2026-10-22; a row with no stamp is nobody's, an
    orphan, whatever address it sits at. One key can hold several rows:
    LiteLLM keeps a second copy when asked twice.
    """

    rows: tuple[LiteLLMDeployment, ...]
    read_at: float
    # The rows grouped by their stamp, built once from ``rows`` so that every
    # lookup by key is a dict hit and the two can never disagree. Build a
    # snapshot through from_rows, never by hand, so the index is always there.
    by_key: dict[DeploymentKey, tuple[LiteLLMDeployment, ...]]

    @classmethod
    def from_rows(cls, rows: tuple[LiteLLMDeployment, ...], *, now: float) -> LiteLLMDeployments:
        """The snapshot of one read, with its by-key index built from the rows.

        The parser calls this with what /model/info answered; a test calls
        it with rows it made up. The unstamped rows stay in ``rows`` and
        appear under no key.
        """
        by_key: dict[DeploymentKey, tuple[LiteLLMDeployment, ...]] = {}
        for row in rows:
            if row.key is not None:
                by_key[row.key] = (*by_key.get(row.key, ()), row)
        return cls(rows, now, by_key)

    def under(self, key: DeploymentKey) -> tuple[LiteLLMDeployment, ...]:
        """The rows stamped with this key, in the order LiteLLM listed them."""
        return self.by_key.get(key, ())

    def has(self, key: DeploymentKey) -> bool:
        return key in self.by_key


def deployments_from_model_info(body: Any, *, now: float) -> LiteLLMDeployments:
    """The one reader of a ``GET /model/info`` answer, whichever shape LiteLLM gave it."""
    out = []
    for row in _rows_of(body):
        if not isinstance(row, dict):
            continue
        params = row.get("litellm_params") or {}
        if not isinstance(params, dict) or not params.get("api_base"):
            continue
        info = row.get("model_info") or {}
        if not isinstance(info, dict):
            info = {}
        model_name = str(row.get("model_name") or "")
        stamp = info.get("llmao_key")
        key = deployment_key(model_name, api_base=str(stamp)) if stamp and model_name else None
        out.append(
            LiteLLMDeployment(
                id=str(info.get("id") or row.get("model_id") or ""),
                model_name=model_name,
                api_base=norm_base(params["api_base"]),
                key=key,
            )
        )
    return LiteLLMDeployments.from_rows(tuple(out), now=now)


def _rows_of(body: Any) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("data") or body.get("models") or []
    return []


@dataclass(frozen=True)
class LiteLLMHealth:
    """LiteLLM's own ``GET /health`` verdict on one deployment (a real completion), and when."""

    healthy: bool
    at: float


@dataclass(frozen=True)
class LiteLLMHealthReport:
    """One ``GET /health`` read: each endpoint as (normalised api_base, LiteLLM model), up or down."""

    healthy: frozenset[tuple[str, str]]
    unhealthy: frozenset[tuple[str, str]]
    read_at: float

    def verdict(self, base: str, litellm_model: str | None = None) -> LiteLLMHealth | None:
        """What the read said about one base, or one (base, model); None when it said nothing.

        Our boxes match on the base alone: one box serves one model. A
        provider's base is shared by every model on the account, so a
        provider deployment matches on both.
        """

        def hit(pairs) -> bool:
            return any(b == base and (litellm_model is None or m == litellm_model) for b, m in pairs)

        if hit(self.healthy):
            return LiteLLMHealth(True, self.read_at)
        if hit(self.unhealthy):
            return LiteLLMHealth(False, self.read_at)
        return None


def health_from_body(body: Any, *, now: float) -> LiteLLMHealthReport:
    """The one reader of a ``GET /health`` answer."""
    body = body if isinstance(body, dict) else {}

    def pairs(name: str) -> frozenset[tuple[str, str]]:
        return frozenset(
            (norm_base(x.get("api_base")), x.get("model"))
            for x in (body.get(name) or [])
            if isinstance(x, dict) and x.get("api_base")
        )

    return LiteLLMHealthReport(pairs("healthy_endpoints"), pairs("unhealthy_endpoints"), now)


class DeploymentManager:
    """Keeps LiteLLM's deployments in step with the fleet.

    Handed the transport to LiteLLM and the fleet. It holds no state of its
    own: what it reads goes to the fleet, which joins it.
    """

    def __init__(self, cfg: Any, fleet: Any, transport: Any):
        self._cfg = cfg
        self.fleet = fleet
        self._transport = transport

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

    async def fetch_deployments(self) -> LiteLLMDeployments:
        """GET /model/info as a snapshot. Fetches only; the fleet is not touched. Raises when LiteLLM is down."""
        resp = await self._transport.request("GET", "model/info")
        self._transport.raise_http(resp)
        return deployments_from_model_info(resp.json(), now=time.time())

    async def add_deployment(self, key: DeploymentKey) -> bool:
        """POST /model/new for the deployment under this key. No serving check. True when a write was sent.

        Callers decide the gate. sync_selfhost waits until vLLM is SERVING.
        POST /do-add-deployment uses add_refusal for the same gate, then calls
        this. A fresh fetch first, so a deployment already present under this
        key is not posted again (a 400 or 409 means the same thing); the
        fetch is folded for everything that reads the fleet, but the decision
        is made on the fetch itself. A fetch after, so presence is observed.
        """
        dep = self.fleet.deployment(key)
        snapshot = await self.fetch_deployments()
        self.fleet.update_litellm_deployments(snapshot)
        if snapshot.has(key):
            _LOGGER.info("model/new already present %s@%s", dep.name, key.display)
            return False
        body = self.deployment_body(dep)
        resp = await self._transport.request("POST", "model/new", json=body)
        if resp.status_code in (400, 409):
            _LOGGER.info("model/new already present %s@%s", dep.name, key.display)
        else:
            self._transport.raise_http(resp)
            _LOGGER.info("model/new %s@%s", dep.name, key.display)
        await self._confirm()
        return True

    async def delete_deployment(self, key: DeploymentKey) -> bool:
        """POST /model/delete for every LiteLLM id under this key. True when a write was sent.

        The ids come from a fresh fetch, by the stamp: a row stamped with
        this key is this deployment at whatever address it was registered,
        including a public port that has since been remapped, and every copy
        goes, since each would take traffic. The fetch is folded either way,
        so a deployment LiteLLM no longer has stops reading as present.
        """
        dep = self.fleet.deployment(key)
        snapshot = await self.fetch_deployments()
        self.fleet.update_litellm_deployments(snapshot)
        ids = [row.id for row in snapshot.under(key) if row.id]
        if not ids:
            _LOGGER.warning("model/delete: no LiteLLM id for %s@%s", dep.name, key.display)
            return False
        for found in ids:
            resp = await self._transport.request("POST", "model/delete", json={"id": found})
            self._transport.raise_http(resp)
            _LOGGER.info("model/delete %s@%s id=%s", dep.name, key.display, found)
        await self._confirm()
        return True

    async def _each(self, action, dep) -> bool:
        """Run `action` on one deployment's key; a failure is logged on the deployment and goes no further.

        The callers walk every deployment. Unguarded, the first one to raise
        ended the walk, and every deployment after it was never registered:
        vLLM up and Healthy, nothing in LiteLLM, the same on every pass.
        Returns whether the action sent a write to LiteLLM.
        """
        try:
            wrote = await action(dep.key)
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
        """Fetch the LiteLLM deployments straight after a write and fold them, so presence is observed, not assumed.

        LiteLLM updates its router inside the /model/new and /model/delete
        handlers before answering (1.105.0: add_new_model awaits
        proxy_config.add_deployment; delete_model calls
        llm_router.delete_deployment), so the fetch that follows sees the
        write. If the fetch fails, the last fold stands and the next pass
        fetches again; a failure here never raises out of the probe tick.
        """
        try:
            snapshot = await self.fetch_deployments()
        except Exception as e:
            _LOGGER.warning("the LiteLLM deployments could not be re-read after a write: %s", e)
            return
        self.fleet.update_litellm_deployments(snapshot)

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
            elif dep.in_litellm and (
                srv.state in (UNHEALTHY, STALLED, REJECTED, FOREIGN) or (srv.unlisted and srv.state != HEALTHY)
            ):
                await self._each(self.delete_deployment, dep)

    async def check_config_skew(self) -> None:
        """Fetch the LiteLLM deployments, fold them into the fleet, and refresh every deployment's badges."""
        self.fleet.update_litellm_deployments(await self.fetch_deployments())
        self.fleet.refresh_skew()

    async def delete_orphan(self, orphan) -> None:
        """POST /model/delete for an orphan, on an admin's Stop tracking. orphan_refusal is the gate."""
        row = orphan.deployment
        resp = await self._transport.request("POST", "model/delete", json={"id": row.id})
        self._transport.raise_http(resp)
        _LOGGER.info("model/delete orphan %s@%s id=%s", row.model_name, row.api_base, row.id)
        await self._confirm()

    async def check_health_skew(self) -> None:
        """Fetch LiteLLM's own GET /health (real completions; tokens), fold it into the fleet, refresh the badges."""
        resp = await self._transport.request("GET", "health")
        self._transport.raise_http(resp)
        body = resp.json() if resp.content else {}
        self.fleet.update_litellm_health(health_from_body(body, now=time.time()))
        self.fleet.refresh_skew()

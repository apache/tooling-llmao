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

"""Deployment lifecycle, signal staleness, skew badges, and model roll-up.

Pure functions. Callers pass probe observations and the fleet intervals from
config.yaml. This module does not probe, and it does not read Fleet objects.

The lifecycle splits into two kinds of thing:

- **Computed state** -- derived here from probes, ``in_litellm`` and
  ``config_served_at``. Recomputed after a control-plane restart; that is what
  this module owns. "Config served" is one of those facts: it is recorded as
  the in-memory ``config_served_at`` timestamp, which anchors the Loading
  window and separates it from the Awaiting pre-state.
- **Admin labels** -- Approved and Reboot requested. In-memory, host-scoped
  annotations the admin applies; they live on ``Fleet`` (see
  ``Fleet.admin_label``), are orthogonal to the computed lifecycle, and are
  lost on a control-plane restart. Retirement is the manual removal of a box
  (drop it from ``fleet.hosts`` and delete its LiteLLM route), not a label.

``health_fail_threshold`` is both the number of failed probes that turn a
healthy server Unhealthy and the number of missed intervals after which a
signal is stale. Stale is Unknown, not Down.

"vLLM is not up yet, so there is no LiteLLM route" is Loading, not skew.
"""

from __future__ import annotations

from dataclasses import dataclass

# Lifecycle. One per deployment, or Unknown for a fleet-key fetch from an IP
# that is not in config.yaml.
# Admin labels (in-memory, host-scoped; stored on Fleet, lost on restart).
# They annotate a box; they are not computed lifecycle states.
APPROVED = "approved"
REBOOT_REQUESTED = "reboot_requested"
ADMIN_LABELS = frozenset({APPROVED, REBOOT_REQUESTED})

AWAITING = "awaiting"
LOADING = "loading"
HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
STALLED = "stalled"
UNKNOWN = "unknown"
CONFIGURED = "configured"

# User-facing roll-up. Only Healthy and Unhealthy count, and only when the
# signal that drives them is fresh.
AVAILABLE = "available"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"

PRIVATE = "private"
EXTERNAL = "external"

SIGNAL_UP = "up"
SIGNAL_DOWN = "down"
SIGNAL_UNKNOWN = "unknown"

SKEW_INTENDED_NOT_IN_LITELLM = "intended_not_in_litellm"
SKEW_VLLM_UP_LITELLM_DOWN = "vllm_up_litellm_down"
SKEW_VLLM_DOWN_LITELLM_UP = "vllm_down_litellm_up"
SKEW_IN_LITELLM_NOT_IN_CONFIG = "in_litellm_not_in_config"
SKEW_CONFIG_MISMATCH = "config_mismatch"

# Display and log text for a badge. The stored value is the badge id.
SKEW_PHRASE = {
    SKEW_INTENDED_NOT_IN_LITELLM: "intended, not in LiteLLM",
    SKEW_VLLM_UP_LITELLM_DOWN: "vLLM up, LiteLLM down",
    SKEW_VLLM_DOWN_LITELLM_UP: "vLLM down, LiteLLM up",
    SKEW_IN_LITELLM_NOT_IN_CONFIG: "in LiteLLM, not in config",
    SKEW_CONFIG_MISMATCH: "config and LiteLLM disagree",
}


@dataclass(frozen=True)
class DeploymentSnapshot:
    """One intended deployment, or one LiteLLM route that config does not list.

    Self-hosted probe fields are the counters ``record_probe`` already keeps.
    ``probe_ok`` is None until the first probe. ``fails`` is the count after
    the probe being described. Commercial rows leave the vLLM fields empty.
    """

    model_name: str
    self_hosted: bool
    probe_ok: bool | None = None
    fails: int = 0
    last_ok: float | None = None
    vllm_probed_at: float | None = None
    in_litellm: bool = False
    litellm_healthy: bool | None = None
    litellm_health_at: float | None = None
    context_window: int | None = None
    kv_cache_tokens: int | None = None
    # LiteLLM has this api_base and config.yaml does not. Not an intended row.
    litellm_only: bool = False
    # Caller-supplied. The skew runner does not detect a model or port mismatch.
    config_mismatch: bool = False
    # When we last handed this box its config. None until a GET /vllm/config
    # succeeded for it since we started. Anchors the Loading window.
    config_served_at: float | None = None
    # Did the last health probe reach vLLM's HTTP server (an answer, e.g. a
    # 503 while the model loads) as opposed to no connection at all? Display
    # only; it does not change the lifecycle. None until the first probe.
    reached: bool | None = None


@dataclass(frozen=True)
class DeploymentStatus:
    model_name: str
    self_hosted: bool
    lifecycle: str
    vllm_signal: str
    litellm_signal: str
    skew: tuple[str, ...]
    context_window: int | None
    kv_cache_tokens: int | None
    counted: bool
    # vLLM answered the last probe (e.g. a 503 while loading) as opposed to no
    # connection. Display-only loading detail; None until the first probe.
    reached: bool | None = None


@dataclass(frozen=True)
class ModelStatus:
    model_name: str
    rollup: str
    privacy: str
    context_window: int | None
    concurrency: int | None


def _stale(probed_at: float | None, now: float, interval_s: float, misses: int) -> bool:
    if probed_at is None:
        return True
    return (now - probed_at) > misses * float(interval_s)


def _signal(up: bool | None, stale: bool) -> str:
    if stale or up is None:
        return SIGNAL_UNKNOWN
    return SIGNAL_UP if up else SIGNAL_DOWN


def self_hosted_lifecycle(
    snap: DeploymentSnapshot,
    cfg: object,
    now: float,
) -> str:
    """Name the vLLM observation.

    A passing probe: Healthy, always -- including the first probe after a
    control-plane restart. No config served yet (``config_served_at`` is None):
    Unhealthy if it is in LiteLLM and the probe failed (it was serving before
    a restart, now down), otherwise Awaiting contact. Config served but not
    yet up: Loading while inside ``health_grace_s`` of the config answer,
    Stalled past it. After it has been Healthy, ``health_fail_threshold``
    failures: Unhealthy; fewer keep Healthy (hold-over-the-threshold).
    """
    grace = float(cfg.health_grace_s)
    threshold = int(cfg.health_fail_threshold)
    if snap.probe_ok is True:
        return HEALTHY
    if snap.config_served_at is None:
        if snap.in_litellm and snap.probe_ok is False:
            return UNHEALTHY
        return AWAITING
    if snap.last_ok is not None:
        return UNHEALTHY if snap.fails >= threshold else HEALTHY
    if snap.probe_ok is None:
        return LOADING
    return LOADING if (now - snap.config_served_at) < grace else STALLED


def commercial_lifecycle(snap: DeploymentSnapshot) -> str:
    if snap.litellm_healthy is True:
        return HEALTHY
    if snap.litellm_healthy is False:
        return UNHEALTHY
    return CONFIGURED


def deployment_status(snap: DeploymentSnapshot, cfg: object, now: float) -> DeploymentStatus:
    if snap.litellm_only:
        lifecycle = UNKNOWN
    elif snap.self_hosted:
        lifecycle = self_hosted_lifecycle(snap, cfg, now)
    else:
        lifecycle = commercial_lifecycle(snap)

    misses = int(cfg.health_fail_threshold)
    vllm_stale = _stale(snap.vllm_probed_at, now, cfg.health_interval_s, misses)
    litellm_stale = _stale(snap.litellm_health_at, now, cfg.litellm_health_interval_s, misses)
    vllm_signal = _signal(snap.probe_ok, vllm_stale) if snap.self_hosted else SIGNAL_UNKNOWN
    litellm_signal = _signal(snap.litellm_healthy, litellm_stale)

    # The signal that decides "is this replica really up" is vLLM for a box
    # and LiteLLM for a commercial endpoint. A stale one drops out of the
    # roll-up instead of reading as down.
    driving_unknown = vllm_signal == SIGNAL_UNKNOWN if snap.self_hosted else litellm_signal == SIGNAL_UNKNOWN
    counted = lifecycle in (HEALTHY, UNHEALTHY) and not driving_unknown and not snap.litellm_only

    skew: list[str] = []
    if snap.litellm_only:
        skew.append(SKEW_IN_LITELLM_NOT_IN_CONFIG)
    elif not snap.in_litellm and lifecycle in (HEALTHY, CONFIGURED):
        skew.append(SKEW_INTENDED_NOT_IN_LITELLM)
    if vllm_signal == SIGNAL_UP and litellm_signal == SIGNAL_DOWN:
        skew.append(SKEW_VLLM_UP_LITELLM_DOWN)
    if vllm_signal == SIGNAL_DOWN and litellm_signal == SIGNAL_UP:
        skew.append(SKEW_VLLM_DOWN_LITELLM_UP)
    if snap.config_mismatch:
        skew.append(SKEW_CONFIG_MISMATCH)

    return DeploymentStatus(
        model_name=snap.model_name,
        self_hosted=snap.self_hosted,
        lifecycle=lifecycle,
        vllm_signal=vllm_signal,
        litellm_signal=litellm_signal,
        skew=tuple(skew),
        context_window=snap.context_window,
        kv_cache_tokens=snap.kv_cache_tokens,
        counted=counted,
        reached=snap.reached,
    )


def unknown_fetch_status() -> str:
    """A fleet key presented from an IP that is not in config.yaml."""
    return UNKNOWN


def model_status(
    model_name: str,
    views: list[DeploymentStatus],
    *,
    catalog_self_hosted: bool,
) -> ModelStatus:
    """Roll up one model. ``views`` may include other models; they are ignored."""
    rows = [v for v in views if v.model_name == model_name]
    counted = [v for v in rows if v.counted]
    healthy = [v for v in counted if v.lifecycle == HEALTHY]
    if not counted or not healthy:
        rollup = UNAVAILABLE
    elif any(v.lifecycle == UNHEALTHY for v in counted):
        rollup = DEGRADED
    else:
        rollup = AVAILABLE

    if not counted:
        privacy = PRIVATE if catalog_self_hosted else EXTERNAL
    elif any(not v.self_hosted for v in counted):
        privacy = EXTERNAL
    else:
        privacy = PRIVATE

    contexts = [v.context_window for v in healthy if v.context_window is not None]
    context = min(contexts) if contexts else None

    slots = []
    for v in healthy:
        if not v.self_hosted or v.kv_cache_tokens is None or not v.context_window:
            continue
        slots.append(v.kv_cache_tokens // v.context_window)
    concurrency = sum(slots) if slots else None

    return ModelStatus(
        model_name=model_name,
        rollup=rollup,
        privacy=privacy,
        context_window=context,
        concurrency=concurrency,
    )

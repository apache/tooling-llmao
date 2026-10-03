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
Approved, Reboot requested, Config served, and Retired are not produced:
nothing records those facts yet.

``health_fail_threshold`` is both the number of failed probes that turn a
healthy server Unhealthy and the number of missed intervals after which a
signal is stale. Stale is Unknown, not Down.

"vLLM is not up yet, so there is no LiteLLM route" is Loading, not skew.
"""

from __future__ import annotations

from dataclasses import dataclass

# Lifecycle. One per deployment, or Unknown for a fleet-key fetch from an IP
# that is not in config.yaml.
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


@dataclass(frozen=True)
class DeploymentSnapshot:
    """One intended deployment, or one LiteLLM route that config does not list.

    Self-hosted probe fields are the counters ``record_probe`` already keeps.
    ``probe_ok`` is None until the first probe. ``fails`` is the count after
    the probe being described. Commercial rows leave the vLLM fields empty.
    """

    model_name: str
    self_hosted: bool
    seen_at: float | None = None
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
    """Name the vLLM observation the same way the probe loop will in step 2.

    Inside ``health_grace_s`` with no successful probe: Loading. Grace expired
    and it never came up: Stalled. A passing probe: Healthy. After it has been
    Healthy, ``health_fail_threshold`` failures: Unhealthy. Fewer failures keep
    Healthy, matching today's hold-over-the-threshold behavior.
    """
    grace = float(cfg.health_grace_s)
    threshold = int(cfg.health_fail_threshold)
    seen = snap.seen_at if snap.seen_at is not None else now
    if snap.probe_ok is None:
        if (now - seen) < grace:
            return LOADING
        return STALLED
    if snap.probe_ok:
        return HEALTHY
    if snap.last_ok is not None:
        if snap.fails >= threshold:
            return UNHEALTHY
        return HEALTHY
    if (now - seen) < grace:
        return LOADING
    return STALLED


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

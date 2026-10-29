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
  the in-memory ``config_served_at`` timestamp, which is the loading start of the Loading
  window and separates it from the Awaiting pre-state.
- **Intent** -- ``fleet.hosts`` and the in-memory approvals (``Fleet.approvals``,
  ``intent.Approval``) say what should run. An approval creates servers; it
  never changes a computed state. Retirement is the manual removal of a box
  (drop it from ``fleet.hosts`` and delete its LiteLLM route), not a state.

``health_fail_threshold`` is both the number of failed probes that turn a
healthy server Unhealthy and the number of missed intervals after which a
signal is stale. Stale is Unknown, not Down.

"vLLM is not up yet, so there is no LiteLLM route" is Loading, not skew.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# Lifecycle. One per deployment, or Unknown for a fleet-key fetch from an IP
# that is not in config.yaml.
AWAITING = "awaiting"
LOADING = "loading"
HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
STALLED = "stalled"
# vLLM answers /health but refuses the key we believe it has on /v1/models.
# Our box with the wrong key, or the salt changed: a person fixes it, and
# nothing but a 200 (or /health going away) ends it. The deployment is
# deleted on the first 401, since every request through it would fail.
REJECTED = "rejected"
# Rejected for REJECTED_PASSES passes running at an address no provider
# listing names any more: something else is serving there. The "remove it
# from fleet.hosts" sentence shows. A host a listing still names is ours
# whatever it answers, so it is never Foreign.
FOREIGN = "foreign"
UNKNOWN = "unknown"
CONFIGURED = "configured"

# Consecutive probe passes a 401 must persist, with the host unlisted, before
# Rejected becomes Foreign. A debounce for a transient refusal (vLLM
# restarting before its key is in place), not time for an admin: about two
# minutes at the 45-second interval. The admin's time comes from Rejected
# having no clock. The third of the pass counts, with fleet.py's
# UNLISTED_PASSES (listing clock) and ORPHAN_PASSES (skew clock).
REJECTED_PASSES = 3

# User-facing roll-up. Only Healthy, Unhealthy and Rejected count, and only
# when the signal that drives them is fresh. Rejected is our box, down for
# traffic; Foreign is not our box, and is left out.
AVAILABLE = "available"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"

PRIVATE = "private"
EXTERNAL = "external"
MIXED = "mixed"

SIGNAL_UP = "up"
SIGNAL_DOWN = "down"
SIGNAL_UNKNOWN = "unknown"

SKEW_INTENDED_NOT_IN_LITELLM = "intended_not_in_litellm"
SKEW_VLLM_UP_LITELLM_DOWN = "vllm_up_litellm_down"
SKEW_VLLM_DOWN_LITELLM_UP = "vllm_down_litellm_up"
SKEW_IN_LITELLM_NOT_IN_CONFIG = "in_litellm_not_in_config"
SKEW_CONFIG_MISMATCH = "config_mismatch"
SKEW_AT_LISTEN_PORT = "in_litellm_at_listen_port"

# Display and log text for a badge. The stored value is the badge id.
SKEW_PHRASE = {
    SKEW_INTENDED_NOT_IN_LITELLM: "intended, not in LiteLLM",
    SKEW_VLLM_UP_LITELLM_DOWN: "vLLM up, LiteLLM down",
    SKEW_VLLM_DOWN_LITELLM_UP: "vLLM down, LiteLLM up",
    SKEW_IN_LITELLM_NOT_IN_CONFIG: "in LiteLLM, not in config",
    SKEW_CONFIG_MISMATCH: "config and LiteLLM disagree",
    SKEW_AT_LISTEN_PORT: "in LiteLLM at the listen port",
}


@dataclass(frozen=True)
class DeploymentSnapshot:
    """One intended deployment, or one LiteLLM route that config does not list.

    Self-hosted probe fields are the counters ``record_probe`` already keeps.
    ``probe_ok`` is None until the first probe. ``fails`` is the count after
    the probe being described. Commercial rows leave the vLLM fields empty.

    Three levels of contact, and ``ok`` is the middle one, not "all is well":
    ``reached`` is any answer from vLLM's HTTP server (a 503 counts);
    ``probe_ok`` is a 200 from /health, which takes no key, so it is vLLM's
    own word that it is serving; ``key_rejected`` is /v1/models' verdict on
    our key. A Rejected box has ``probe_ok`` True.
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
    # LiteLLM has this server at its listen port, which is unreachable off-box.
    at_listen_port: bool = False
    # When we last handed this box its config. None until a GET /vllm/config
    # succeeded for it since we started. The loading start.
    config_served_at: float | None = None
    # Did the last health probe reach vLLM's HTTP server (an answer, e.g. a
    # 503 while the model loads) as opposed to no connection at all? None
    # until the first probe. Mostly display; with no loading start it decides
    # between Awaiting and Stalled once the grace has run out.
    reached: bool | None = None
    # When this process first tried to probe the server. The clock for a
    # server that has no loading start and never answers: Awaiting until
    # health_grace_s after this, then Stalled. None until the first attempt.
    first_probed_at: float | None = None
    # The last /v1/models answer with the working key: True is a 401, False
    # is a 200, None is not asked yet or no usable answer. Read only when the
    # probe passes; a box that does not answer /health has no key to judge.
    key_rejected: bool | None = None
    # Consecutive probe passes the key has been refused. Reset by a 200, and
    # by a failed probe, which interrupts the run.
    rejected_passes: int = 0
    # The host verdict from the provider listings (Fleet.place_servers).
    # Foreign needs it; nothing else here reads it.
    unlisted: bool = False


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
    # LiteLLM has this route and config.yaml does not. It may be one of our
    # boxes. Any such row forces model privacy to MIXED.
    litellm_only: bool = False


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
    control-plane restart -- unless /v1/models refused the working key:
    Rejected, and Foreign once the refusal has run ``REJECTED_PASSES`` passes
    at a host no listing names. No config served yet (``config_served_at`` is None):
    Unhealthy if it is in LiteLLM and the probe failed (it was serving before
    a restart, now down); Stalled if nothing has answered at all for
    ``health_grace_s`` after the first probe attempt (a rental that died, with
    no deployment left to say so); otherwise Awaiting contact. Config served
    but not yet up: Loading while inside ``health_grace_s`` of the config
    answer, Stalled past it. After it has been Healthy,
    ``health_fail_threshold`` failures: Unhealthy; fewer keep Healthy
    (hold-over-the-threshold).

    The not-reached clock runs from the first attempt, not from unlisted's
    listing count: a new RunPod box is unlisted within minutes but fetches
    its config only after its image pull, and must not read Stalled five
    minutes after the admin created it. A probe that reaches (a 503) is not
    on this clock; it stays Awaiting until the config fetch starts Loading.
    """
    grace = float(cfg.health_grace_s)
    threshold = int(cfg.health_fail_threshold)
    if snap.probe_ok is True:
        if snap.key_rejected is True:
            if snap.unlisted and snap.rejected_passes >= REJECTED_PASSES:
                return FOREIGN
            return REJECTED
        return HEALTHY
    if snap.config_served_at is None:
        if snap.in_litellm and snap.probe_ok is False:
            return UNHEALTHY
        not_reached = snap.probe_ok is False and snap.reached is False
        if not_reached and snap.first_probed_at is not None and (now - snap.first_probed_at) >= grace:
            return STALLED
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


def deployment_status(snap: DeploymentSnapshot, cfg: object, now: float | None = None) -> DeploymentStatus:
    now = now or time.time()
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
    counted = lifecycle in (HEALTHY, UNHEALTHY, REJECTED) and not driving_unknown and not snap.litellm_only

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
    if snap.at_listen_port:
        skew.append(SKEW_AT_LISTEN_PORT)

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
        litellm_only=snap.litellm_only,
    )


def _privacy(rows: list[DeploymentStatus]) -> str:
    """Where requests for this model can be handled.

    Every configured row counts, including one that is down and therefore not
    in the roll-up. A LiteLLM-only route might be one of our boxes or a third
    party; until we can tell, the model is MIXED.
    """
    if any(v.litellm_only for v in rows):
        return MIXED
    if not rows:
        # A provider's model always has its deployment. One with none is
        # waiting for a box of ours.
        return PRIVATE
    hosted = any(v.self_hosted for v in rows)
    external = any(not v.self_hosted for v in rows)
    if hosted and external:
        return MIXED
    if hosted:
        return PRIVATE
    return EXTERNAL


def unknown_fetch_status() -> str:
    """A fleet key presented from an IP that is not in config.yaml."""
    return UNKNOWN


def model_status(
    model_name: str,
    views: list[DeploymentStatus],
) -> ModelStatus:
    """Roll up one model. ``views`` may include other models; they are ignored."""
    rows = [v for v in views if v.model_name == model_name]
    counted = [v for v in rows if v.counted]
    healthy = [v for v in counted if v.lifecycle == HEALTHY]
    if not counted or not healthy:
        rollup = UNAVAILABLE
    elif any(v.lifecycle in (UNHEALTHY, REJECTED) for v in counted):
        rollup = DEGRADED
    else:
        rollup = AVAILABLE

    privacy = _privacy(rows)

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

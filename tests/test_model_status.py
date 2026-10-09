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

"""Status roll-up. No Fleet objects and no probes."""

from types import SimpleNamespace

from llmao.model_status import (
    AVAILABLE,
    AWAITING,
    CONFIGURED,
    DEGRADED,
    EXTERNAL,
    HEALTHY,
    LOADING,
    MIXED,
    PRIVATE,
    SKEW_CONFIG_MISMATCH,
    SKEW_IN_LITELLM_NOT_IN_CONFIG,
    SKEW_INTENDED_NOT_IN_LITELLM,
    SKEW_VLLM_DOWN_LITELLM_UP,
    SKEW_VLLM_UP_LITELLM_DOWN,
    STALLED,
    UNAVAILABLE,
    UNHEALTHY,
    UNKNOWN,
    DeploymentSnapshot,
    deployment_status,
    model_status,
    unknown_fetch_status,
)

# Same keys config.yaml fleet.* already has. Not the design doc's round numbers.
CFG = SimpleNamespace(
    health_interval_s=45,
    litellm_health_interval_s=14400,
    health_grace_s=1800,
    health_fail_threshold=3,
)
NOW = 10_000.0
MODEL = "qwen3-8b"


def _self(**kw) -> DeploymentSnapshot:
    base = dict(
        model_name=MODEL,
        self_hosted=True,
        config_served_at=NOW - 10,
        probe_ok=True,
        fails=0,
        last_ok=NOW,
        vllm_probed_at=NOW,
        in_litellm=True,
        litellm_healthy=True,
        litellm_health_at=NOW,
        context_window=40960,
        kv_cache_tokens=40960 * 4,
    )
    base.update(kw)
    return DeploymentSnapshot(**base)


def test_fleet_fixture_rollup_is_degraded():
    """3 healthy, 3 loading, 1 unhealthy, one unknown fetch. One healthy row is skewed."""
    views = []
    for i in range(3):
        extra = {}
        if i == 0:
            extra = {"litellm_healthy": False}
        views.append(deployment_status(_self(context_window=40960 - i, **extra), CFG, NOW))
    for _ in range(3):
        views.append(
            deployment_status(
                _self(
                    probe_ok=None,
                    last_ok=None,
                    vllm_probed_at=None,
                    in_litellm=False,
                    litellm_healthy=None,
                    litellm_health_at=None,
                    config_served_at=NOW - 60,
                ),
                CFG,
                NOW,
            )
        )
    views.append(
        deployment_status(
            _self(probe_ok=False, fails=CFG.health_fail_threshold, last_ok=NOW - 100, litellm_healthy=True),
            CFG,
            NOW,
        )
    )

    lifecycles = [v.lifecycle for v in views]
    assert lifecycles.count(HEALTHY) == 3
    assert lifecycles.count(LOADING) == 3
    assert lifecycles.count(UNHEALTHY) == 1
    assert views[0].skew == (SKEW_VLLM_UP_LITELLM_DOWN,)
    assert all(not v.skew for v in views[1:4])
    assert unknown_fetch_status() == UNKNOWN

    summary = model_status(MODEL, views, catalog_self_hosted=True)
    assert summary.rollup == DEGRADED
    assert summary.privacy == PRIVATE
    assert summary.context_window == 40958
    # Three healthy replicas, four full-context slots each. The unhealthy one is not added.
    assert summary.concurrency == 12


def test_loading_is_not_a_missing_route_badge():
    view = deployment_status(
        _self(
            probe_ok=False,
            fails=1,
            last_ok=None,
            in_litellm=False,
            litellm_healthy=None,
            litellm_health_at=None,
        ),
        CFG,
        NOW,
    )
    assert view.lifecycle == LOADING
    assert view.skew == ()
    assert view.counted is False


def test_grace_expired_without_success_is_stalled():
    view = deployment_status(
        _self(probe_ok=False, fails=1, last_ok=None, config_served_at=NOW - CFG.health_grace_s),
        CFG,
        NOW,
    )
    assert view.lifecycle == STALLED
    assert view.counted is False


def test_no_config_served_is_awaiting():
    # No GET /vllm/config since we started: the box has not contacted us.
    view = deployment_status(_self(config_served_at=None, probe_ok=None, last_ok=None, in_litellm=False), CFG, NOW)
    assert view.lifecycle == AWAITING
    assert view.counted is False
    assert view.skew == ()


def test_restart_recovery_was_serving_now_down():
    # We restarted, no config served yet, but LiteLLM still lists the route
    # and the probe fails: this was serving, so Unhealthy, not Awaiting.
    view = deployment_status(
        _self(config_served_at=None, probe_ok=False, fails=CFG.health_fail_threshold, last_ok=None),
        CFG,
        NOW,
    )
    assert view.lifecycle == UNHEALTHY
    assert view.counted is True
    assert view.skew == (SKEW_VLLM_DOWN_LITELLM_UP,)


def test_awaiting_needs_no_probe():
    # Same as awaiting, but the first probe already failed and the box is not
    # in LiteLLM: still Awaiting contact, the failure is expected.
    view = deployment_status(
        _self(config_served_at=None, probe_ok=False, fails=1, last_ok=None, in_litellm=False),
        CFG,
        NOW,
    )
    assert view.lifecycle == AWAITING


def test_config_served_within_grace_is_loading():
    view = deployment_status(
        _self(
            config_served_at=NOW - 10,
            probe_ok=False,
            fails=1,
            last_ok=None,
            in_litellm=False,
            litellm_healthy=None,
            litellm_health_at=None,
        ),
        CFG,
        NOW,
    )
    assert view.lifecycle == LOADING
    assert view.counted is False


def test_healthy_probe_wins_without_config_served():
    # The box answers a health probe (e.g. it was booted out-of-band). Healthy
    # comes from the probe alone; the missing fetch does not hold it back.
    view = deployment_status(_self(config_served_at=None, in_litellm=False, litellm_healthy=None), CFG, NOW)
    assert view.lifecycle == HEALTHY


def test_reached_does_not_change_lifecycle():
    # 503 while the model loads versus no connection: both are Loading; the
    # detail only changes what the page says.
    for reached in (True, False):
        view = deployment_status(
            _self(probe_ok=False, fails=1, last_ok=None, in_litellm=False, reached=reached),
            CFG,
            NOW,
        )
        assert view.lifecycle == LOADING
        assert view.reached is reached


def test_under_fail_threshold_stays_healthy():
    view = deployment_status(
        _self(probe_ok=False, fails=CFG.health_fail_threshold - 1, last_ok=NOW - 50),
        CFG,
        NOW,
    )
    assert view.lifecycle == HEALTHY
    assert view.counted is True


def test_stale_vllm_is_excluded():
    age = CFG.health_fail_threshold * CFG.health_interval_s + 1
    view = deployment_status(_self(vllm_probed_at=NOW - age), CFG, NOW)
    assert view.lifecycle == HEALTHY
    assert view.vllm_signal == "unknown"
    assert view.counted is False
    summary = model_status(MODEL, [view], catalog_self_hosted=True)
    assert summary.rollup == UNAVAILABLE
    assert summary.concurrency is None
    assert summary.privacy == PRIVATE


def test_commercial_configured_is_not_counted_and_both_kinds_are_mixed():
    commercial = deployment_status(
        DeploymentSnapshot(model_name=MODEL, self_hosted=False, context_window=200_000),
        CFG,
        NOW,
    )
    assert commercial.lifecycle == CONFIGURED
    assert commercial.counted is False
    assert commercial.skew == (SKEW_INTENDED_NOT_IN_LITELLM,)

    healthy_box = deployment_status(_self(), CFG, NOW)
    up_commercial = deployment_status(
        DeploymentSnapshot(
            model_name=MODEL,
            self_hosted=False,
            in_litellm=True,
            litellm_healthy=True,
            litellm_health_at=NOW,
            context_window=8000,
        ),
        CFG,
        NOW,
    )
    summary = model_status(MODEL, [healthy_box, up_commercial], catalog_self_hosted=True)
    assert summary.privacy == MIXED
    # The external row is configured even while LiteLLM does not have it.
    down_commercial = deployment_status(
        DeploymentSnapshot(model_name=MODEL, self_hosted=False, context_window=200_000),
        CFG,
        NOW,
    )
    assert model_status(MODEL, [healthy_box, down_commercial], catalog_self_hosted=True).privacy == MIXED
    assert model_status(MODEL, [up_commercial], catalog_self_hosted=False).privacy == EXTERNAL
    assert summary.rollup == AVAILABLE
    # Minimum across healthy deployments. The commercial window is the smaller one.
    assert summary.context_window == 8000
    # Commercial capacity is not a KV-cache figure.
    assert summary.concurrency == 4


def test_extra_route_and_mismatch_are_badges_only():
    extra = deployment_status(
        DeploymentSnapshot(model_name=MODEL, self_hosted=True, litellm_only=True, config_mismatch=True),
        CFG,
        NOW,
    )
    assert extra.lifecycle == UNKNOWN
    assert extra.counted is False
    assert extra.skew == (SKEW_IN_LITELLM_NOT_IN_CONFIG, SKEW_CONFIG_MISMATCH)
    summary = model_status(MODEL, [extra], catalog_self_hosted=False)
    assert summary.rollup == UNAVAILABLE
    assert summary.privacy == MIXED
    hosted = deployment_status(_self(), CFG, NOW)
    assert model_status(MODEL, [hosted, extra], catalog_self_hosted=True).privacy == MIXED


def test_vllm_down_litellm_up():
    view = deployment_status(
        _self(probe_ok=False, fails=CFG.health_fail_threshold, last_ok=NOW - 20, litellm_healthy=True),
        CFG,
        NOW,
    )
    assert view.lifecycle == UNHEALTHY
    assert view.skew == (SKEW_VLLM_DOWN_LITELLM_UP,)

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
"""Tiers: what a key of each kind is allowed.

Two layers. Config gives every tier its limits, and an admin can change them
at runtime; a change is kept in LiteLLM's memory store and laid over config.

This matters more than most because the failure is silent. A key minted
without limits looks identical to a limited one until something saturates
the fleet.
"""

import asyncio
import json

import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.proxy.client import TIER_FREE, TIER_PROJECT, TIER_SERVICE, MemoryStore, TierManager, resolve_entitlement
from tests.fake_transport import fake_memory


def _cfg(**over):
    base = {
        "entitlements": {
            "default": {"rpm_limit": 60, "tpm_limit": 60000, "max_parallel_requests": 4},
            "free_tier": {
                "rpm_limit": 30,
                "tpm_limit": 30000,
                "max_parallel_requests": 2,
                "token_cap": 1000000,
                "token_window_days": 7,
            },
            "project": {"rpm_limit": 120, "tpm_limit": 120000, "max_parallel_requests": 8},
            "service": {"rpm_limit": 240, "tpm_limit": 300000, "max_parallel_requests": 8},
        }
    }
    base.update(over)
    return edict(base)


# ---------------------------------------------------------------------------
# What config says
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tier,expected",
    [
        (TIER_FREE, {"rpm_limit": 30, "tpm_limit": 30000, "max_parallel_requests": 2}),
        (TIER_PROJECT, {"rpm_limit": 120, "tpm_limit": 120000, "max_parallel_requests": 8}),
        (TIER_SERVICE, {"rpm_limit": 240, "tpm_limit": 300000, "max_parallel_requests": 8}),
    ],
)
def test_tier_overrides_default(tier, expected):
    assert resolve_entitlement(_cfg(), tier) == expected


def test_tier_inherits_what_it_does_not_name():
    """A tier raising one ceiling must not have to restate the others.

    Otherwise every tier carries a full copy of default, and the next person
    to change default silently fails to change three of the four tiers.
    """
    cfg = _cfg()
    cfg.entitlements.sparse = edict({"rpm_limit": 999})
    assert resolve_entitlement(cfg, "sparse") == {
        "rpm_limit": 999,  # named
        "tpm_limit": 60000,  # inherited
        "max_parallel_requests": 4,
    }


def test_unknown_tier_falls_back_to_default():
    assert resolve_entitlement(_cfg(), "no-such-tier") == {
        "rpm_limit": 60,
        "tpm_limit": 60000,
        "max_parallel_requests": 4,
    }


def test_absent_config_yields_no_limits():
    """Documents the dangerous case rather than pretending it cannot happen.

    LiteLLM's default for every limit is unlimited, so a config without an
    entitlements block mints keys that can saturate the fleet. The function
    returns {} honestly; config.yaml shipping with values is what prevents
    it, and this test is here so nobody "fixes" the empty return into a
    guess.
    """
    assert resolve_entitlement(edict({}), TIER_FREE) == {}


def test_every_shipped_tier_sets_all_three_limits():
    """A tier missing a limit is a tier that is unlimited in that dimension."""
    cfg = _cfg()
    for tier in (TIER_FREE, TIER_PROJECT, TIER_SERVICE):
        got = resolve_entitlement(cfg, tier)
        for field in ("rpm_limit", "tpm_limit", "max_parallel_requests"):
            assert got.get(field), f"{tier} leaves {field} unlimited"


# ---------------------------------------------------------------------------
# What an admin has changed: TierManager
# ---------------------------------------------------------------------------


def _tiers(*, down=False):
    """A TierManager over a stand-in memory store, and what the store holds."""
    transport, store = fake_memory(down=down)
    cfg = edict(
        entitlements=edict(
            default=edict(rpm_limit=60, tpm_limit=60000, max_parallel_requests=4),
            free_tier=edict(token_cap=1_000_000, token_window_days=7),
        )
    )
    return TierManager(cfg, MemoryStore(transport)), store


def _run(coro):
    return asyncio.run(coro)


def test_tier_limits_are_config_until_an_admin_changes_them():
    t, store = _tiers()
    assert _run(t.limits("free_tier"))["token_cap"] == 1_000_000
    got = _run(t.set_limits("free_tier", token_cap=250_000, by="admin1"))
    assert got["token_cap"] == 250_000
    assert got["token_window_days"] == 7, "what was not changed still comes from config"
    assert list(store) == ["llmao:tiers"]


def test_changing_one_tier_leaves_another_alone():
    """Every tier shares the one row."""
    t, store = _tiers()
    _run(t.set_limits("free_tier", token_cap=250_000, by="admin1"))
    _run(t.set_limits("service", rpm_limit=10, by="admin2"))
    _run(t.set_limits("free_tier", token_window_days=3, by="admin2"))
    held = json.loads(store["llmao:tiers"])
    assert {k: v for k, v in held["free_tier"].items() if k != "changed_at"} == {
        "token_cap": 250_000,
        "token_window_days": 3,
        "changed_by": "admin2",
    }
    assert {k: v for k, v in held["service"].items() if k != "changed_at"} == {"rpm_limit": 10, "changed_by": "admin2"}


def test_a_tier_says_who_changed_it_last_and_its_limits_do_not():
    t, _ = _tiers()
    assert _run(t.last_change("free_tier")) is None
    _run(t.set_limits("free_tier", token_cap=250_000, by="admin1"))
    change = _run(t.last_change("free_tier"))
    assert change.by == "admin1"
    assert change.at.startswith("20")
    limits = _run(t.limits("free_tier"))
    assert "changed_by" not in limits and "changed_at" not in limits


def test_tier_limits_fall_back_to_config_when_litellm_is_down():
    """A page must still have limits to show during a proxy restart."""
    assert _run(_tiers(down=True)[0].limits("free_tier"))["token_cap"] == 1_000_000


def test_a_new_keys_limits_include_what_an_admin_has_changed():
    """The Tiers page says a change applies to keys issued from then on."""
    t, _ = _tiers()
    assert _run(t.key_limits("service")) == {"rpm_limit": 60, "tpm_limit": 60000, "max_parallel_requests": 4}
    _run(t.set_limits("service", rpm_limit=10, token_cap=5_000_000, by="admin1"))
    limits = _run(t.key_limits("service"))
    assert limits == {"rpm_limit": 10, "tpm_limit": 60000, "max_parallel_requests": 4}
    assert "token_cap" not in limits, "LiteLLM holds rate limits on a key, not our token cap"

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

"""Entitlement resolution and the rolling token allowance.

These matter more than most because the failure is silent in both
directions. A key minted without limits looks identical to a limited one
until something saturates the fleet; an allowance that never ages out looks
identical to a working one until somebody's week does not reset.
"""

import datetime as dt

import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.litellm_client import TIER_FREE, TIER_PROJECT, TIER_SERVICE, resolve_entitlement
from llmao.token_window import RollingAllowance

UTC = dt.UTC


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
# Resolution
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
# The rolling allowance
# ---------------------------------------------------------------------------

DAY0 = dt.datetime(2026, 9, 1, 12, tzinfo=UTC)


def _at(days):
    return DAY0 + dt.timedelta(days=days)


def test_spend_accumulates_within_the_window():
    a = RollingAllowance(cap=1_000_000)
    for d in range(5):
        a.spend(200_000, _at(d))
    assert a.used(_at(4)) == 1_000_000
    assert a.remaining(_at(4)) == 0
    assert a.exhausted(_at(4))


def test_usage_ages_out_gradually():
    """The property a calendar reset does not have.

    Five days of 200k, then three days later the first three days have aged
    out and 600k has come back -- without an instant where everyone's counter
    dropped at once.
    """
    a = RollingAllowance(cap=1_000_000)
    for d in range(5):
        a.spend(200_000, _at(d))
    assert a.used(_at(8)) == 600_000
    assert a.remaining(_at(8)) == 400_000


def test_a_burst_is_allowed_and_recovers_whole():
    """One allowance, not a daily entitlement.

    Spending the week's share in an hour is permitted -- that is how people
    work -- and it returns in full after the window rather than in daily
    instalments.
    """
    a = RollingAllowance(cap=1_000_000)
    a.spend(1_000_000, DAY0)
    assert a.exhausted(DAY0)
    assert a.exhausted(_at(6))
    assert not a.exhausted(_at(7))
    assert a.remaining(_at(7)) == 1_000_000


def test_no_boundary_to_straddle():
    """The exploit a fixed weekly reset permits.

    Under a Monday reset, 1M on Sunday night and 1M on Monday morning is two
    allowances in twelve hours. Here the second spend sees the first.
    """
    a = RollingAllowance(cap=1_000_000)
    sunday_late = dt.datetime(2026, 9, 6, 23, 0, tzinfo=UTC)
    monday_early = dt.datetime(2026, 9, 7, 0, 30, tzinfo=UTC)
    a.spend(1_000_000, sunday_late)
    assert a.exhausted(monday_early), "a day boundary must not refill the allowance"


def test_returns_says_when_tokens_come_back():
    """A fixed window answers this trivially and a live rolling sum cannot.

    Keeping the answer is the reason for daily buckets over an aggregate
    query: "340k left" is not actionable without "and 200k returns Tuesday".
    """
    a = RollingAllowance(cap=1_000_000)
    a.spend(200_000, _at(0))
    a.spend(300_000, _at(1))
    got = a.returns(_at(1))
    assert got == [
        (dt.date(2026, 9, 8), 200_000),
        (dt.date(2026, 9, 9), 300_000),
    ]


def test_buckets_do_not_grow_without_bound():
    """Thirty days of use must not leave thirty counters behind."""
    a = RollingAllowance(cap=1_000_000, window_days=7)
    for d in range(30):
        a.spend(1000, _at(d))
    assert len(a.buckets) <= 7


def test_spend_records_an_overshoot_rather_than_refusing():
    """Recording happens after a call completes, so the last request of an
    allowance can exceed it. Deliberate: refusing mid-stream would mean a
    half-finished answer, and the overshoot is bounded by one request."""
    a = RollingAllowance(cap=1000)
    a.spend(5000, DAY0)
    assert a.used(DAY0) == 5000
    assert a.remaining(DAY0) == 0  # clamped, never negative
    assert a.exhausted(DAY0)


def test_zero_and_negative_spend_are_ignored():
    a = RollingAllowance(cap=1000)
    a.spend(0, DAY0)
    a.spend(-50, DAY0)
    assert a.used(DAY0) == 0


def test_cap_comes_from_config_not_storage():
    """An admin lowering a tier must affect existing principals immediately.

    A cap rehydrated from stored state would keep granting the old allowance
    to everyone who already had one, which is the opposite of what a runtime
    control is for.
    """
    stored = RollingAllowance(cap=5_000_000, window_days=7)
    stored.spend(900_000, DAY0)
    revived = RollingAllowance.from_dict(stored.to_dict(), cap=1_000_000)
    assert revived.cap == 1_000_000
    assert revived.used(DAY0) == 900_000
    assert revived.remaining(DAY0) == 100_000


def test_round_trips_through_dict():
    a = RollingAllowance(cap=1_000_000)
    a.spend(123_456, DAY0)
    b = RollingAllowance.from_dict(a.to_dict(), cap=1_000_000)
    assert b.used(DAY0) == a.used(DAY0)


def test_window_length_is_honoured():
    a = RollingAllowance(cap=1_000_000, window_days=1)
    a.spend(1_000_000, DAY0)
    assert a.exhausted(DAY0)
    assert not a.exhausted(_at(1))


# ---------------------------------------------------------------------------
# Contention sampling
#
# The pilot's one unanswerable-after-the-fact question: did anyone wait?
# ---------------------------------------------------------------------------


def test_parse_metrics_reads_the_four_figures():
    from llmao.contention import parse_metrics

    text = "\n".join(
        [
            "# HELP vllm:num_requests_running whatever",
            'vllm:num_requests_running{engine="0",model_name="x"} 4.0',
            'vllm:num_requests_waiting{engine="0",model_name="x"} 2.0',
            'vllm:gpu_cache_usage_perc{engine="0",model_name="x"} 0.31',
            'vllm:num_preemptions_total{engine="0",model_name="x"} 3.0',
        ]
    )
    got = parse_metrics("gemma4-26b", text, now=1.0).as_dict()
    assert got["running"] == 4.0
    assert got["waiting"] == 2.0
    assert got["preempted"] == 3.0
    # exposed as a fraction, reported as a percentage
    assert got["kv_pct"] == 31.0


def test_parse_metrics_survives_an_unexpected_body():
    """A box returning HTML, or nothing, must not stop the sampler."""
    from llmao.contention import parse_metrics

    for body in ("", "<html>503</html>", "vllm:num_requests_running{} notanumber"):
        got = parse_metrics("x", body, now=1.0)
        assert got.running == 0.0

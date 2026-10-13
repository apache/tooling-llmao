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

"""The rolling token allowance (llmao/token_window.py).

The failure here is silent: an allowance that never ages out looks identical
to a working one until somebody's week does not reset.
"""

import datetime as dt

from llmao.token_window import RollingAllowance

UTC = dt.UTC

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

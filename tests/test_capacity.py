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
"""The hourly capacity rollup and where its rows are kept (llmao/capacity.py)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.capacity import (
    HOUR_S,
    CapacityStore,
    CapacityWriter,
    HourlyRollup,
    days_to_read,
    hour_label,
    retention_days,
    summarise_capacity,
    summarise_spend,
)
from llmao.proxy.client import MemoryStore
from llmao.vllm_client import Sample
from tests.fake_transport import fake_memory

# 2026-10-18T09:00:00Z, a clock hour's start.
T0 = 1792314000.0
SERVER = "203.0.113.10:8001"


def _sample(ts, *, running=0, waiting=0, kv=0.0, **counters):
    buckets = counters.pop("buckets", {})
    return Sample(ts=ts, running=running, waiting=waiting, kv=kv, counters=counters, buckets=buckets)


def _store():
    transport, rows = fake_memory()
    return CapacityStore(MemoryStore(transport)), rows


def test_t0_is_on_the_hour():
    assert T0 % HOUR_S == 0
    assert hour_label(int(T0)) == "2026-10-18T09"


def test_an_hour_of_readings_becomes_one_row():
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "qwen3-8b", _sample(T0, running=2, kv=0.2, prompt_tokens=100, generation_tokens=10))
    rollup.record(
        SERVER, "qwen3-8b", _sample(T0 + 15, running=1, waiting=3, kv=0.6, prompt_tokens=400, generation_tokens=60)
    )
    rollup.record(SERVER, "qwen3-8b", _sample(T0 + 30, kv=0.1, prompt_tokens=450, generation_tokens=70))
    (hour,) = rollup.take_finished(T0 + HOUR_S)
    row = hour.row(15)
    assert row.hour == "2026-10-18T09"
    assert row.server == SERVER
    assert row.model_name == "qwen3-8b"
    assert row.covered_s == 45
    assert row.busy_frac == round(2 / 3, 3)
    assert row.waiting_frac == round(1 / 3, 3)
    assert row.waiting_peak == 3
    assert row.kv_avg == 0.3
    assert row.kv_peak == 0.6
    # Growth since the first reading; what came before it is not this hour's.
    assert row.prompt_tokens == 350
    assert row.generation_tokens == 60
    assert row.usd_per_hour is None


def test_a_restarted_server_counts_from_zero():
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "m", _sample(T0, requests=50))
    rollup.record(SERVER, "m", _sample(T0 + 15, requests=60))
    rollup.record(SERVER, "m", _sample(T0 + 30, requests=4))  # vLLM restarted
    (hour,) = rollup.take_finished(T0 + HOUR_S)
    assert hour.row(15).requests == 14


def test_a_missed_reading_does_not_credit_the_gap():
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "m", _sample(T0, requests=10))
    rollup.missed(SERVER)
    rollup.record(SERVER, "m", _sample(T0 + 600, requests=500))
    (hour,) = rollup.take_finished(T0 + HOUR_S)
    assert hour.row(15).requests == 0
    assert hour.row(15).covered_s == 30


def test_the_hour_is_finished_by_the_next_reading_or_by_the_clock():
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "m", _sample(T0))
    rollup.record("198.51.100.8:8001", "m", _sample(T0))
    assert rollup.take_finished(T0 + 60) == []
    # The first server moves into the next hour; the second goes quiet.
    rollup.record(SERVER, "m", _sample(T0 + HOUR_S))
    assert [h.server for h in rollup.take_finished(T0 + HOUR_S + 1)] == [SERVER, "198.51.100.8:8001"]
    # The new hour is still open.
    assert rollup.take_finished(T0 + HOUR_S + 2) == []


def test_queue_time_p95_comes_from_the_histogram():
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "m", _sample(T0, queued=0, queue_s=0, buckets={0.3: 0, 1.0: 0, 5.0: 0}))
    rollup.record(SERVER, "m", _sample(T0 + 15, queued=100, queue_s=50, buckets={0.3: 80, 1.0: 96, 5.0: 100}))
    (hour,) = rollup.take_finished(T0 + HOUR_S)
    row = hour.row(15)
    assert row.queue_p95_s == 1.0
    assert row.queue_avg_s == 0.5


def test_a_quiet_hour_has_no_queue_figures():
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "m", _sample(T0))
    (hour,) = rollup.take_finished(T0 + HOUR_S)
    assert hour.row(15).queue_p95_s is None
    assert hour.row(15).queue_avg_s is None


def test_rows_are_kept_by_hour_then_server_and_read_by_day():
    store, rows = _store()
    rollup = HourlyRollup(interval_s=15)
    rollup.record(SERVER, "m", _sample(T0))
    (hour,) = rollup.take_finished(T0 + HOUR_S)
    asyncio.run(store.write(hour.row(15)))
    assert list(rows) == [f"llmao:capacity:2026-10-18T09:{SERVER}"]
    today = dt.date(2026, 10, 18)
    assert [r.server for r in asyncio.run(store.read(1, today=today))] == [SERVER]
    assert asyncio.run(store.read(1, today=today + dt.timedelta(days=1))) == []
    assert len(asyncio.run(store.read(2, today=today + dt.timedelta(days=1)))) == 1


def test_pruning_drops_only_the_days_past_retention():
    store, rows = _store()
    for day in ("2026-07-19", "2026-07-20", "2026-07-21"):
        rows[f"llmao:capacity:{day}T00:{SERVER}"] = json.dumps({"hour": f"{day}T00"})
    # Keeping 90 days on 2026-10-18 keeps 2026-07-21 onwards.
    gone = asyncio.run(store.prune(90, today=dt.date(2026, 10, 18)))
    assert gone == 2
    assert list(rows) == [f"llmao:capacity:2026-07-21T00:{SERVER}"]


def test_an_hour_that_cannot_be_stored_waits_for_the_next_pass():
    transport, rows = fake_memory(down=True)
    rollup = HourlyRollup(interval_s=15)
    writer = CapacityWriter(rollup, CapacityStore(MemoryStore(transport)))
    rollup.record(SERVER, "m", _sample(T0))
    asyncio.run(writer.write_finished(T0 + HOUR_S))
    assert rows == {}
    transport.down = False
    asyncio.run(writer.write_finished(T0 + HOUR_S + 60))
    assert list(rows) == [f"llmao:capacity:2026-10-18T09:{SERVER}"]


def test_retention_is_required():
    assert retention_days(edict(capacity_retention_days=90)) == 90
    with pytest.raises(ValueError, match="capacity_retention_days is required"):
        retention_days(edict())
    for bad in (0, -1, "ninety"):
        with pytest.raises(ValueError, match="capacity_retention_days must be"):
            retention_days(edict(capacity_retention_days=bad))


# ---- provider spend ----

SPEND_TODAY = dt.date(2026, 10, 19)


def _row(day, provider, model, project, usd):
    return edict(day=day, provider=provider, model_name=model, project=project, spend_usd=usd, tokens=0, requests=0)


def test_enough_days_are_read_for_the_month_to_date():
    assert days_to_read(7, SPEND_TODAY) == 19
    assert days_to_read(30, SPEND_TODAY) == 30
    assert days_to_read(7, dt.date(2026, 10, 3)) == 7


def test_a_provider_is_totalled_over_the_period_today_and_the_month():
    rows = [
        _row("2026-09-30", "xai", "grok-4", "tooling", 9.0),  # last month: not in the month to date
        _row("2026-10-01", "xai", "grok-4", "tooling", 4.0),
        _row("2026-10-12", "xai", "grok-4", "mahout", 2.0),  # in the month, outside a 7-day period
        _row("2026-10-18", "xai", "grok-4", "tooling", 3.0),
        _row("2026-10-19", "xai", "grok-4-mini", None, 1.0),
    ]
    (xai,) = summarise_spend(rows, today=SPEND_TODAY, period_days=7)
    assert xai.provider == "xai"
    assert xai.period_usd == 4.0
    assert xai.today_usd == 1.0
    assert xai.month_to_date_usd == 10.0
    # 10 over 19 days, for a 31-day month
    assert xai.projected_month_usd == round(10.0 / 19 * 31, 2)
    assert [(m.name, m.usd) for m in xai.models] == [("grok-4", 3.0), ("grok-4-mini", 1.0)]
    assert [(p.name, p.usd) for p in xai.projects] == [("tooling", 3.0), ("(no project)", 1.0)]
    assert [(d.day, d.usd) for d in xai.daily] == [("2026-10-18", 3.0), ("2026-10-19", 1.0)]


def test_providers_are_listed_by_spend_largest_first():
    rows = [
        _row("2026-10-19", "openrouter", "gemma4-26b", "tooling", 0.5),
        _row("2026-10-19", "xai", "grok-4", "x", 2.0),
    ]
    assert [p.provider for p in summarise_spend(rows, today=SPEND_TODAY, period_days=7)] == ["xai", "openrouter"]


def test_nothing_bought_is_nothing_to_show():
    assert summarise_spend([], today=SPEND_TODAY, period_days=30) == []


# ---- the self-hosted report ----


def _hour_row(
    hour, server, model, *, usd=0.1, covered=3600, busy=0.5, waiting=0.0, kv=0.2, prompt=1000, gen=100, q95=None
):
    return edict(
        hour=hour,
        server=server,
        model_name=model,
        covered_s=covered,
        busy_frac=busy,
        waiting_frac=waiting,
        waiting_peak=1 if waiting else 0,
        kv_avg=kv / 2,
        kv_peak=kv,
        prompt_tokens=prompt,
        generation_tokens=gen,
        requests=10,
        preemptions=0,
        queue_avg_s=None,
        queue_p95_s=q95,
        usd_per_hour=usd,
    )


def test_a_model_is_costed_by_the_hour_and_weighed_by_what_was_sampled():
    rows = [
        _hour_row("2026-10-19T08", "203.0.113.10:8001", "qwen3-8b", usd=0.5, covered=900, busy=1.0, q95=0.3),
        _hour_row("2026-10-19T09", "203.0.113.10:8001", "qwen3-8b", usd=0.5, covered=2700, busy=0.0, q95=2.5),
        _hour_row("2026-10-18T09", "198.51.100.8:8001", "qwen3-8b", usd=0.25, kv=0.9),
        _hour_row("2026-10-19T09", "192.0.2.4:8001", "gemma4-26b", usd=1.0),
        # outside a 7-day period ending 2026-10-19
        _hour_row("2026-10-12T09", "198.51.100.8:8001", "qwen3-8b", usd=99.0),
    ]
    report = summarise_capacity(rows, today=dt.date(2026, 10, 19), period_days=7)
    _gemma, qwen = sorted(report.models, key=lambda m: m.model_name)
    # A whole hour of rental for every hour sampled, however much of it was.
    assert qwen.cost_usd == 1.25
    # Over the three clock hours seen, as a rate per day.
    assert qwen.cost_per_day == 1.25 / 3 * 24
    assert qwen.hours == 3
    # Busy over 900s + 2700s + 3600s sampled: (900*1.0 + 3600*0.5) / 7200.
    assert qwen.busy == (900 * 1.0 + 3600 * 0.5) / 7200
    assert qwen.kv_peak == 0.9
    assert qwen.queue_p95_worst_s == 2.5
    assert qwen.usd_per_mtok == 1.25 / (3 * 1100 / 1e6)
    assert [(d.day, d.value) for d in qwen.daily_tokens] == [("2026-10-18", 1100), ("2026-10-19", 2200)]
    assert [s.server for s in qwen.servers] == ["198.51.100.8:8001", "203.0.113.10:8001"]
    # The costliest model first; the fleet's day is every model's.
    assert [m.model_name for m in report.models] == ["qwen3-8b", "gemma4-26b"]
    assert report.cost_usd == 2.25
    assert report.hours_seen == 3
    assert report.cost_per_day == 2.25 / 3 * 24


def test_an_unpriced_hour_is_counted_as_unpriced_not_as_free():
    rows = [
        _hour_row("2026-10-19T08", "203.0.113.10:8001", "m", usd=None),
        _hour_row("2026-10-19T09", "203.0.113.10:8001", "m", usd=0.5),
    ]
    (m,) = summarise_capacity(rows, today=dt.date(2026, 10, 19), period_days=7).models
    assert m.unpriced_hours == 1
    assert m.cost_usd == 0.5
    # A cost per token would understate with an hour missing, so there is none.
    assert m.usd_per_mtok is None


def test_no_rows_is_an_empty_report():
    report = summarise_capacity([], today=dt.date(2026, 10, 19), period_days=30)
    assert report.models == []
    assert report.cost_per_day == 0.0

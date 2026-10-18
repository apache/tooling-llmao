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

from llmao.capacity import (
    HOUR_S,
    CapacityStore,
    CapacityWriter,
    HourlyRollup,
    Sample,
    hour_label,
)
from llmao.proxy.client import MemoryStore
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

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

"""What each self-hosted server did, hour by hour, for capacity planning.

Whether to add a box or return one is a question about weeks: how busy each
server was, how often requests queued, how full its KV cache got, and how
many tokens it served. See docs/capacity-design.md for the whole design.

Four parts, each knowing one thing:

  sampler (llmao/sampler.py)  reads every serving box's /metrics and hands
                              each reading to the rollup
  HourlyRollup                turns readings into one row per server per
                              clock hour; no I/O, so all the arithmetic is
                              testable on its own
  CapacityStore               the only part that knows the /v1/memory keys:
                              writes rows, reads days, prunes old days
  CapacityWriter, ...Pruner   the two runners: write finished hours, and
                              delete days past retention

The writer is not part of the sampler's loop on purpose. Writing goes to
LiteLLM; a slow or absent LiteLLM must not stall sampling, so finished hours
wait in memory until it answers.

An llmao restart loses the hour in progress. Each row says how much of its
hour was sampled (covered_s), and the loss is accepted: the report is read
for trends over weeks.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from asfpy.stopwatch import Stopwatch
from easydict import EasyDict as edict  # noqa: N813

_LOGGER = logging.getLogger(__name__)

KEY_PREFIX = "llmao:capacity:"
HOUR_S = 3600
DAY_S = 86400


@dataclass
class Sample:
    """One reading of one server's /metrics, as the sampler parsed it.

    Gauges are the value at the moment of reading. Counters and the
    queue-time histogram are vLLM's totals since the server started; the
    rollup differences them.
    """

    ts: float
    running: float = 0.0
    waiting: float = 0.0
    # KV cache in use, as a fraction.
    kv: float = 0.0
    # prompt_tokens, generation_tokens, requests, preemptions, queue_s, queued
    counters: dict[str, float] = field(default_factory=dict)
    # Queue-time histogram: upper bound in seconds => cumulative count.
    buckets: dict[float, float] = field(default_factory=dict)
    # The KV cache size in tokens, fixed at engine start. Not rolled up: the
    # sampler hands it to the fleet for the oversized check.
    kv_cache_tokens: int | None = None


def retention_days(cfg: Any) -> int:
    """config.yaml capacity_retention_days: how long hourly rows are kept. Call at startup.

    Required, like every other setting with an operational effect: a
    missing value is a startup error, not a quiet default.
    """
    if "capacity_retention_days" not in cfg:
        raise ValueError("config.yaml: capacity_retention_days is required")
    raw = cfg.capacity_retention_days
    try:
        days = int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError("config.yaml: capacity_retention_days must be a whole number of days") from e
    if days <= 0:
        raise ValueError("config.yaml: capacity_retention_days must be > 0")
    return days


def _growth(now: float, before: float) -> float:
    """How far a counter moved between two readings. A drop is a restart, counted from zero."""
    return now - before if now >= before else now


def hour_of(ts: float) -> int:
    """The start of the clock hour `ts` falls in, as epoch seconds."""
    return int(ts // HOUR_S) * HOUR_S


def hour_label(hour: int) -> str:
    """2026-10-18T09, in UTC. Sorts as text, and a day is a prefix of it."""
    return dt.datetime.fromtimestamp(hour, dt.UTC).strftime("%Y-%m-%dT%H")


@dataclass
class Hour:
    """One server's clock hour, built up a reading at a time."""

    hour: int
    server: str
    model_name: str
    readings: int = 0
    busy: int = 0
    queueing: int = 0
    waiting_peak: float = 0.0
    kv_sum: float = 0.0
    kv_peak: float = 0.0
    counters: dict[str, float] = field(default_factory=dict)
    buckets: dict[float, float] = field(default_factory=dict)
    # This server's share of its box's rental, dollars per hour. None when
    # no provider prices the box.
    usd_per_hour: float | None = None

    def add(self, sample: Sample, before: Sample | None) -> None:
        self.readings += 1
        self.busy += sample.running > 0
        self.queueing += sample.waiting > 0
        self.waiting_peak = max(self.waiting_peak, sample.waiting)
        self.kv_sum += sample.kv
        self.kv_peak = max(self.kv_peak, sample.kv)
        if before is None:
            # Nothing to difference against: what the counters held before
            # this reading accrued before llmao was looking.
            return
        for key, value in sample.counters.items():
            self.counters[key] = self.counters.get(key, 0.0) + _growth(value, before.counters.get(key, 0.0))
        for bound, value in sample.buckets.items():
            self.buckets[bound] = self.buckets.get(bound, 0.0) + _growth(value, before.buckets.get(bound, 0.0))

    def queue_p95_s(self) -> float | None:
        """The histogram bound under which 95% of this hour's queued requests fell."""
        total = self.counters.get("queued", 0.0)
        if total <= 0:
            return None
        for bound in sorted(self.buckets):
            if self.buckets[bound] >= 0.95 * total:
                return bound
        return None

    def row(self, interval_s: float) -> edict:
        """The stored form, one per server per hour."""
        n = max(self.readings, 1)
        queued = self.counters.get("queued", 0.0)

        def count(key: str) -> int:
            return round(self.counters.get(key, 0.0))

        return edict(
            hour=hour_label(self.hour),
            server=self.server,
            model_name=self.model_name,
            # How much of the hour was sampled: short after an llmao
            # restart, or while the box did not answer.
            covered_s=int(min(self.readings * interval_s, HOUR_S)),
            busy_frac=round(self.busy / n, 3),
            waiting_frac=round(self.queueing / n, 3),
            waiting_peak=int(self.waiting_peak),
            kv_avg=round(self.kv_sum / n, 3),
            kv_peak=round(self.kv_peak, 3),
            prompt_tokens=count("prompt_tokens"),
            generation_tokens=count("generation_tokens"),
            requests=count("requests"),
            preemptions=count("preemptions"),
            queue_avg_s=round(self.counters.get("queue_s", 0.0) / queued, 3) if queued else None,
            queue_p95_s=self.queue_p95_s(),
            usd_per_hour=self.usd_per_hour,
        )


class HourlyRollup:
    """Readings in, finished hours out. Fed by the sampler, drained by the writer."""

    def __init__(self, interval_s: float):
        self.interval_s = interval_s
        self._hours: dict[str, Hour] = {}
        self._before: dict[str, Sample] = {}
        self._finished: list[Hour] = []

    def record(self, server: str, model_name: str, sample: Sample, *, usd_per_hour: float | None = None) -> None:
        """Add one reading to its server's hour. `usd_per_hour` is the server's
        share of its box's rental, when the box's provider prices it."""
        hour = hour_of(sample.ts)
        current = self._hours.get(server)
        if current is None or current.hour != hour:
            if current is not None:
                self._finished.append(current)
            current = self._hours[server] = Hour(hour=hour, server=server, model_name=model_name)
        current.add(sample, self._before.get(server))
        if usd_per_hour is not None:
            current.usd_per_hour = round(usd_per_hour, 4)
        self._before[server] = sample

    def missed(self, server: str) -> None:
        """The box did not answer. Its counters will have moved by the time it
        does, and that growth cannot be placed in any one hour, so the next
        reading starts afresh."""
        self._before.pop(server, None)

    def take_finished(self, now: float) -> list[Hour]:
        """Every hour that is over, handed to the caller and forgotten here.

        An hour is over when a later one replaced it, or when the clock has
        moved past it for a server that has gone quiet.
        """
        this_hour = hour_of(now)
        for server, current in list(self._hours.items()):
            if current.hour < this_hour:
                self._finished.append(current)
                del self._hours[server]
        done, self._finished = self._finished, []
        return done

    def put_back(self, hours: list[Hour]) -> None:
        """Hours the writer could not store, kept for its next attempt."""
        self._finished = hours + self._finished


class CapacityStore:
    """The hourly rows in LiteLLM's /v1/memory, keyed by hour then server:

        llmao:capacity:2026-10-18T09:203.0.113.10:8001

    Handed the MemoryStore.
    """

    def __init__(self, memory: Any):
        self._memory = memory

    async def write(self, row: edict) -> None:
        await self._memory.put(f"{KEY_PREFIX}{row.hour}:{row.server}", json.dumps(row, sort_keys=True))

    async def _day(self, day: dt.date) -> list[edict]:
        return [edict(item) for item in await self._memory.find(f"{KEY_PREFIX}{day.isoformat()}T")]

    async def read(self, days: int, *, today: dt.date | None = None) -> list[edict]:
        """Every row of the last `days` days, today included.

        One request per day, all at once. A day holds 24 rows per server, so
        it fits one page of the memory API (500) up to twenty servers.
        """
        today = today or dt.datetime.now(dt.UTC).date()
        wanted = [today - dt.timedelta(days=n) for n in range(days)]
        with Stopwatch(name=f"capacity read of {days} days"):
            found = await asyncio.gather(*(self._day(day) for day in wanted))
        rows = []
        for item in (item for day in found for item in day):
            try:
                rows.append(edict(json.loads(item.get("value") or "")))
            except (TypeError, ValueError):
                continue
        return rows

    async def prune(self, keep_days: int, *, today: dt.date | None = None, sweep_days: int = 7) -> int:
        """Delete the rows of the `sweep_days` days just past retention.

        Run daily, each day is swept seven times before it slips out of
        reach, so a few days with llmao down still get cleared.
        """
        today = today or dt.datetime.now(dt.UTC).date()
        oldest_kept = today - dt.timedelta(days=keep_days - 1)
        gone = 0
        for n in range(1, sweep_days + 1):
            for item in await self._day(oldest_kept - dt.timedelta(days=n)):
                if item.get("key"):
                    await self._memory.delete(item.key)
                    gone += 1
        return gone


class CapacityWriter:
    """Runner: stores each finished hour. A failed write is kept and retried."""

    def __init__(self, rollup: HourlyRollup, store: CapacityStore, *, every_s: float = 60):
        self._rollup = rollup
        self._store = store
        self._every = every_s

    async def write_finished(self, now: float) -> None:
        pending = self._rollup.take_finished(now)
        for i, hour in enumerate(pending):
            try:
                await self._store.write(hour.row(self._rollup.interval_s))
            except Exception as e:
                # LiteLLM away: keep this hour and the rest for the next pass.
                _LOGGER.warning("capacity: could not store %s %s (%s)", hour.server, hour_label(hour.hour), e)
                self._rollup.put_back(pending[i:])
                return

    async def run(self) -> None:
        while True:
            try:
                await self.write_finished(time.time())
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("capacity: writing failed")
            await asyncio.sleep(self._every)


class CapacityPruner:
    """Runner: once a day, deletes rows older than the retention period."""

    def __init__(self, store: CapacityStore, keep_days: int):
        self._store = store
        self._keep_days = keep_days

    async def run(self) -> None:
        while True:
            try:
                gone = await self._store.prune(self._keep_days)
                if gone:
                    _LOGGER.info("capacity: pruned %d rows older than %d days", gone, self._keep_days)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("capacity: pruning failed")
            await asyncio.sleep(DAY_S)

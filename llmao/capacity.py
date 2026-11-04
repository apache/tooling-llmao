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

Also here: demand, what was asked of the gateway per model per hour, from
LiteLLM's spend log (fold_demand, DemandRollup), the other half of the
same rows. What the rows say, the per-model summaries the page reads, is
llmao/planning.py.

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
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pathlib

    from llmao.rental import Rental
    from llmao.vllm_client import Sample

from asfpy.stopwatch import Stopwatch
from easydict import EasyDict as edict  # noqa: N813

from llmao import prompt_log
from llmao.prompt_log import counts_as_demand, percentile

_LOGGER = logging.getLogger(__name__)

KEY_PREFIX = "llmao:capacity:"
# Demand rows (what was asked, per model per hour) and the per-day marker
# that says a day has been rolled up. See "demand", below.
DEMAND_PREFIX = "llmao:demand:"
DEMAND_DAY_PREFIX = "llmao:demand-day:"
HOUR_S = 3600
DAY_S = 86400


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
    # The machine, as its provider names it (Rental.ident, "Vast 53865410")
    # and labels it ("RTX 3060 x2"). Empty when no provider has placed the
    # server. The server is an address and port, which a provider changes
    # when it moves the machine and which means nothing once the rental has
    # ended; the stamp is what keeps a rental's hours together through both.
    rental: str = ""
    rental_label: str = ""

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
            rental=self.rental,
            rental_label=self.rental_label,
        )


class HourlyRollup:
    """Readings in, finished hours out. Fed by the sampler, drained by the writer."""

    def __init__(self, interval_s: float):
        self.interval_s = interval_s
        self._hours: dict[str, Hour] = {}
        self._before: dict[str, Sample] = {}
        self._finished: list[Hour] = []

    def record(
        self,
        server: str,
        model_name: str,
        sample: Sample,
        *,
        usd_per_hour: float | None = None,
        rental: Rental | None = None,
    ) -> None:
        """Add one reading to its server's hour. `usd_per_hour` is the server's
        share of its box's rental, when the box's provider prices it; `rental`
        is the machine the provider places the server on, when one does."""
        hour = hour_of(sample.ts)
        current = self._hours.get(server)
        if current is None or current.hour != hour:
            if current is not None:
                self._finished.append(current)
            current = self._hours[server] = Hour(hour=hour, server=server, model_name=model_name)
        current.add(sample, self._before.get(server))
        if usd_per_hour is not None:
            current.usd_per_hour = round(usd_per_hour, 4)
        if rental is not None:
            current.rental = rental.ident
            current.rental_label = rental.label
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

    async def _day(self, prefix: str, day: dt.date) -> list[edict]:
        return [edict(item) for item in await self._memory.find(f"{prefix}{day.isoformat()}T")]

    async def _read(self, prefix: str, days: int, today: dt.date | None) -> list[edict]:
        today = today or dt.datetime.now(dt.UTC).date()
        wanted = [today - dt.timedelta(days=n) for n in range(days)]
        with Stopwatch(name=f"{prefix} read of {days} days"):
            found = await asyncio.gather(*(self._day(prefix, day) for day in wanted))
        rows = []
        for item in (item for day in found for item in day):
            try:
                rows.append(edict(json.loads(item.get("value") or "")))
            except (TypeError, ValueError):
                continue
        return rows

    async def read(self, days: int, *, today: dt.date | None = None) -> list[edict]:
        """Every capacity row of the last `days` days, today included.

        One request per day, all at once. A day holds 24 rows per server, so
        it fits one page of the memory API (500) up to twenty servers.
        """
        return await self._read(KEY_PREFIX, days, today)

    # --- demand: what was asked, per model per hour. Written once per
    # closed day by DemandRollup; read by /admin/capacity beside the
    # capacity rows. A day holds 24 rows per model: one page up to twenty
    # models.

    async def write_demand(self, row: edict) -> None:
        await self._memory.put(f"{DEMAND_PREFIX}{row.hour}:{row.model}", json.dumps(row, sort_keys=True))

    async def read_demand(self, days: int, *, today: dt.date | None = None) -> list[edict]:
        """Every demand row of the last `days` days, today included."""
        return await self._read(DEMAND_PREFIX, days, today)

    async def demand_days_done(self) -> dict[str, edict]:
        """Day -> what the rollup wrote for it ({sources, rows}); the days to skip."""
        done = {}
        for item in await self._memory.find(DEMAND_DAY_PREFIX):
            try:
                done[str(item.get("key") or "").removeprefix(DEMAND_DAY_PREFIX)] = edict(
                    json.loads(item.get("value") or "")
                )
            except (TypeError, ValueError):
                continue
        return done

    async def mark_demand_day(self, day: str, *, sources: list[str], rows: int, log: str = "off") -> None:
        """Record that `day` has been rolled up, by which fold, from which sources, into how many rows.

        `log` is what became of the prompt log side: "ok", "absent" (no
        file for the day), "failed" (the tool did not answer; tried again
        next run), or "off" (no log directory configured).
        """
        marker = json.dumps(dict(version=FOLD_VERSION, sources=sources, rows=rows, log=log), sort_keys=True)
        await self._memory.put(f"{DEMAND_DAY_PREFIX}{day}", marker)

    async def clear_demand_day(self, day: dt.date) -> int:
        """Delete a day's demand rows, before it is rolled up again. The count."""
        gone = 0
        for item in await self._day(DEMAND_PREFIX, day):
            if item.get("key"):
                await self._memory.delete(item.key)
                gone += 1
        return gone

    async def prune(self, keep_days: int, *, today: dt.date | None = None, sweep_days: int = 7) -> int:
        """Delete the rows of the `sweep_days` days just past retention.

        Capacity rows, demand rows and the day's rollup marker alike. Run
        daily, each day is swept seven times before it slips out of reach,
        so a few days with llmao down still get cleared.
        """
        today = today or dt.datetime.now(dt.UTC).date()
        oldest_kept = today - dt.timedelta(days=keep_days - 1)
        gone = 0
        for n in range(1, sweep_days + 1):
            day = oldest_kept - dt.timedelta(days=n)
            for prefix in (KEY_PREFIX, DEMAND_PREFIX):
                for item in await self._day(prefix, day):
                    if item.get("key"):
                        await self._memory.delete(item.key)
                        gone += 1
            await self._memory.delete(f"{DEMAND_DAY_PREFIX}{day.isoformat()}")
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


# ---- demand ---------------------------------------------------------------
#
# What was asked of the gateway, per model per UTC hour, from LiteLLM's
# spend log: the other side of the capacity rows, which say what each
# server did. Keyed llmao:demand:<hour>:<model>, so the two join on hour and
# model. Design: docs/prompt-logging-design.md, "Demand rows".
#
# Everything LiteLLM has that the rows might need moves into the store when
# a day is rolled up, once; the prompt log later fills only the fields
# LiteLLM lacks (streamed, with_tools, finish_length, time to first token),
# which are absent from a row until it does.
#
# What counts as demand: a call to a model. A spend row with no model group
# (a bad key refused at the door, a /v1/models listing) is not use of a
# model, and LiteLLM's own health checks, which run as their own team, are
# not anyone's demand. Both are left out; measured on 2026-11-01 they were
# nearly half of the day's rows.

# What counts is decided once, in llmao/prompt_log.py (counts_as_demand),
# so the log side of a row agrees with this side. The deployment-id case
# was seen on 2026-11-02: sixty ids over 281 rows, eight tokens each.

# Bumped whenever fold_demand changes what a row holds or what counts. A
# day whose marker carries an older version is rolled up again, so a
# correction here reaches every stored day on the next run.
FOLD_VERSION = 2

# Prompt length in tokens, bucketed: is anyone using the context we pay KV
# cache for. The last bucket has no upper bound.
PROMPT_BUCKETS = ((1_000, "<1k"), (8_000, "1k-8k"), (32_000, "8k-32k"), (128_000, "32k-128k"), (None, ">128k"))
NO_PROJECT = "(no project)"


def prompt_bucket(tokens: int) -> str:
    for limit, name in PROMPT_BUCKETS:
        if limit is None or tokens < limit:
            return name
    return PROMPT_BUCKETS[-1][1]


def tool_family(user_agent: str | None) -> str:
    """The tool behind a user agent: the product before the first slash.

    "opencode/latest/2.2.1/acp" is opencode, "OpenAI/Python 1.109.1" is the
    OpenAI SDK, "codex-tui/0.166.0 (...)" is codex-tui. Every browser says
    Mozilla first, so those fold into one; a request with no user agent is
    "unknown". This is the same cut LiteLLM makes for its User-Agent tags.
    """
    name = (user_agent or "").split("/", 1)[0].strip()
    if not name:
        return "unknown"
    return "browser" if name == "Mozilla" else name


def add_counts(
    bucket: dict, name: str, *, requests: int = 1, prompt_tokens: int = 0, completion_tokens: int = 0
) -> None:
    if name not in bucket:
        bucket[name] = dict(requests=0, prompt_tokens=0, completion_tokens=0)
    # Looked up after the assignment: an EasyDict stores a COPY of a dict
    # assigned into it, so the one created above is not the one it holds.
    acc = bucket[name]
    acc["requests"] += requests
    acc["prompt_tokens"] += prompt_tokens
    acc["completion_tokens"] += completion_tokens


def _refusal(meta: dict) -> str:
    """Why LiteLLM refused a request, as one word it already chose."""
    info = meta.get("error_information") or {}
    return str(info.get("normalized_error") or info.get("error_code") or info.get("error_class") or "unknown")


def _empty_demand_row(hour: str, model: str) -> edict:
    """A demand row before either source has written to it."""
    return edict(
        hour=hour,
        model=model,
        sources=[],
        requests=0,
        prompt_tokens=0,
        completion_tokens=0,
        by_project={},
        by_tool={},
        refused={},
        prompt_len={},
        duration_p50_s=None,
        duration_p95_s=None,
    )


def fold_demand(day: dt.date, spend_rows: list) -> list[edict]:
    """A day of LiteLLM's spend log as demand rows, one per model per hour.

    Pure: rows in, rows out, so the arithmetic is testable on its own.
    `requests` counts every call to the model, refused ones included;
    `refused` says how many and why, so refused is a share of requests.
    Tokens and durations come from the successful calls only: a refusal
    carries no tokens and takes no time worth measuring.
    """
    by_hour: dict[tuple[str, str], list] = defaultdict(list)
    for r in spend_rows:
        meta = r.metadata or edict()
        start = str(r.startTime or "")
        if not counts_as_demand(r.model_group, meta.get("user_api_key_team_alias")) or start[:10] != day.isoformat():
            continue
        by_hour[(start[:13], r.model_group)].append((r, meta))

    out = []
    for (hour, model), items in sorted(by_hour.items()):
        row = _empty_demand_row(hour, model)
        durations = []
        for r, meta in items:
            refused = r.status == "failure"
            prompt = 0 if refused else int(r.prompt_tokens or 0)
            completion = 0 if refused else int(r.completion_tokens or 0)
            row.requests += 1
            row.prompt_tokens += prompt
            row.completion_tokens += completion
            counted = dict(prompt_tokens=prompt, completion_tokens=completion)
            add_counts(row.by_project, meta.get("user_api_key_team_alias") or NO_PROJECT, **counted)
            add_counts(row.by_tool, tool_family(meta.get("user_agent")), **counted)
            if refused:
                reason = _refusal(meta)
                row.refused[reason] = row.refused.get(reason, 0) + 1
                continue
            bucket = prompt_bucket(prompt)
            row.prompt_len[bucket] = row.prompt_len.get(bucket, 0) + 1
            if r.request_duration_ms is not None:
                durations.append(int(r.request_duration_ms) / 1000)
        row.sources = ["litellm"]
        row.duration_p50_s = percentile(durations, 0.5)
        row.duration_p95_s = percentile(durations, 0.95)
        out.append(row)
    return out


def _seconds_until(hour: int, now: dt.datetime) -> float:
    """Seconds from `now` to the next `hour`:00 UTC, never zero."""
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


class DemandRollup:
    """Runner: once a day, the demand rows for every closed day the store lacks.

    Runs at start and then at `at_hour`:00 UTC, an hour after the day
    closes: the grace the daily usage reader also allows for LiteLLM's
    background flush. Each run lists the closed days inside retention,
    subtracts the days the store has a marker for, and rolls up each
    missing one, oldest first. On a normal day that is yesterday; after
    downtime it is several, done in the same run. The current day is never
    read, and nothing is kept between runs.

    A day with no traffic is marked too, with no rows, or it would be asked
    for again every day.

    With a `log_dir`, each day's rollup also spawns the prompt log tool for
    that day (prompt_log.rollup_day) and folds what only the log can say
    into the same rows. A day whose log side failed is written from
    LiteLLM alone, noted for the admins through `notices`, and tried again
    next run; a day with no log file is final.
    """

    def __init__(
        self,
        metering: Any,
        store: CapacityStore,
        keep_days: int,
        *,
        at_hour: int = 1,
        grace_s: int = HOUR_S,
        log_dir: pathlib.Path | None = None,
        notices: Any = None,
        tool: pathlib.Path = prompt_log.TOOL,
    ):
        self._metering = metering
        self._store = store
        self._keep_days = keep_days
        self._at_hour = at_hour
        self._grace_s = grace_s
        self._log_dir = log_dir
        self._notices = notices
        self._tool = tool

    def missing_days(self, done: dict[str, edict], now: dt.datetime) -> list[dt.date]:
        """Closed days inside retention with no marker, or one from an older fold; oldest first."""
        last_closed = (now - dt.timedelta(seconds=self._grace_s)).date() - dt.timedelta(days=1)
        day = now.date() - dt.timedelta(days=self._keep_days - 1)
        out = []
        while day <= last_closed:
            marker = done.get(day.isoformat())
            if (
                marker is None
                or marker.get("version") != FOLD_VERSION
                or (self._log_dir is not None and marker.get("log") == "failed")
            ):
                out.append(day)
            day += dt.timedelta(days=1)
        return out

    async def roll_up(self, day: dt.date) -> int:
        """One day: read from LiteLLM, fold, add the log's side, replace what the store had, mark it. The row count."""
        rows = fold_demand(day, await self._metering.spend_log(day))
        log = "off" if self._log_dir is None else await self._fold_log(day, rows)
        # Cleared first: a row the new fold no longer produces (a model that
        # no longer counts) would otherwise outlive it under its old key.
        await self._store.clear_demand_day(day)
        for row in rows:
            await self._store.write_demand(row)
        sources = ["litellm", "log"] if log == "ok" else ["litellm"]
        await self._store.mark_demand_day(day.isoformat(), sources=sources, rows=len(rows), log=log)
        return len(rows)

    async def _fold_log(self, day: dt.date, rows: list[edict]) -> str:
        """The prompt log's side of the day into `rows`; "ok", "absent" or "failed"."""
        try:
            result = await prompt_log.rollup_day(day, self._log_dir, tool=self._tool)
        except prompt_log.RollupError as e:
            _LOGGER.warning("demand: the prompt log side of %s failed: %s", day, e)
            if self._notices is not None:
                await self._notices.note(
                    f"The prompt log rollup for {day} failed ({e}). That day's demand rows are from LiteLLM"
                    " alone; the next run tries the log again.",
                    subject="prompt-log-rollup",
                )
            return "failed"
        if not result.found:
            return "absent"
        by_key = {(r.hour, r.model): r for r in rows}
        for h in result.hours:
            row = by_key.get((h.hour, h.model))
            if row is None:
                # The log saw a request LiteLLM has no row for: the
                # reconciliation's other direction, kept as a row of its own.
                row = by_key[(h.hour, h.model)] = _empty_demand_row(h.hour, h.model)
                rows.append(row)
            row.update(
                log_requests=h.requests,
                streamed=h.streamed,
                with_tools=h.with_tools,
                finish_length=h.finish_length,
                ttft_p50_s=h.ttft_p50_s,
                ttft_p95_s=h.ttft_p95_s,
            )
            row.sources = [*row.sources, "log"]
        return "ok"

    async def roll_up_missing(self, now: dt.datetime) -> list[dt.date]:
        """Every missing closed day, oldest first; stops at the first that fails. The days done."""
        done = await self._store.demand_days_done()
        rolled = []
        for day in self.missing_days(done, now):
            try:
                n = await self.roll_up(day)
            except Exception as e:
                # LiteLLM away, most likely: the rest wait for the next run too.
                _LOGGER.warning("demand: could not roll up %s (%s)", day, e)
                break
            _LOGGER.info("demand: %s rolled up into %d rows", day, n)
            rolled.append(day)
        return rolled

    async def run(self) -> None:
        while True:
            try:
                await self.roll_up_missing(dt.datetime.now(dt.UTC))
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("demand: rollup failed")
            await asyncio.sleep(_seconds_until(self._at_hour, dt.datetime.now(dt.UTC)))

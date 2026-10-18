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

"""Read every serving box's vLLM /metrics, and hand each reading on.

The one reader of /metrics on our boxes. Every sample_interval_s it fetches
all of them at once, parses what capacity planning needs, and gives each
reading to the hourly rollup (llmao/capacity.py), which knows nothing of
HTTP. Nothing here is stored or logged per reading.

Queue depth is only visible while it is happening -- the spend log records
what ran, not what waited -- so it has to be sampled or it is gone. The
rollup's waiting fraction is what shows whether requests actually queue,
and so whether a model needs another box.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from llmao.capacity import HourlyRollup, Sample

_LOGGER = logging.getLogger(__name__)

# Instant values. Metric names with the vllm: prefix stripped.
_GAUGES = {
    "num_requests_running": "running",
    "num_requests_waiting": "waiting",
    "kv_cache_usage_perc": "kv",
}
# vLLM's totals since it started. request_success_total has a line per
# finish reason, so lines of one name are summed.
_COUNTERS = {
    "prompt_tokens_total": "prompt_tokens",
    "generation_tokens_total": "generation_tokens",
    "request_success_total": "requests",
    "num_preemptions_total": "preemptions",
    "request_queue_time_seconds_sum": "queue_s",
    "request_queue_time_seconds_count": "queued",
}
_QUEUE_BUCKET = "request_queue_time_seconds_bucket"


def sample_interval(cfg: Any) -> int:
    """Seconds between readings: config.yaml sample_interval_s. Call at startup.

    Sampling is never off, so a missing or non-positive value is a startup
    error. contention_interval_s, the old name, is still read, with a
    warning: p6 writes config.yaml and moves on its own schedule.
    """
    if "sample_interval_s" in cfg:
        raw = cfg.sample_interval_s
    elif "contention_interval_s" in cfg:
        _LOGGER.warning("config.yaml: contention_interval_s is deprecated; rename it to sample_interval_s")
        raw = cfg.contention_interval_s
    else:
        raise ValueError("config.yaml: sample_interval_s is required")
    try:
        interval = int(raw)
    except (TypeError, ValueError) as e:
        raise ValueError("config.yaml: sample_interval_s must be a whole number of seconds") from e
    if interval <= 0:
        raise ValueError("config.yaml: sample_interval_s must be > 0; sampling is never off")
    return interval


def _labels(inner: str) -> dict[str, str]:
    labels = {}
    for part in inner.split(","):
        key, _, value = part.partition("=")
        if key:
            labels[key.strip()] = value.strip().strip('"')
    return labels


def parse_metrics(text: str, *, now: float | None = None) -> Sample:
    """What the rollup needs, from vLLM's Prometheus exposition text.

    Hand-parsed rather than with a client library: a dozen metrics in one
    format. Anything unreadable is skipped, so a box answering with HTML or
    nothing gives an empty reading rather than an error.
    """
    out = Sample(ts=now if now is not None else time.time())
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, brace, rest = line.partition("{")
        if brace:
            inner, _, value = rest.partition("} ")
        else:
            inner = ""
            name, _, value = line.partition(" ")
        name = name.strip().removeprefix("vllm:")
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            continue
        if name in _GAUGES:
            setattr(out, _GAUGES[name], getattr(out, _GAUGES[name]) + number)
        elif name in _COUNTERS:
            key = _COUNTERS[name]
            out.counters[key] = out.counters.get(key, 0.0) + number
        elif name == _QUEUE_BUCKET:
            try:
                bound = float(_labels(inner).get("le", ""))
            except ValueError:
                continue
            out.buckets[bound] = out.buckets.get(bound, 0.0) + number
    return out


class MetricsSampler:
    """Runner: reads every serving box on an interval and feeds the rollup.

    A box that does not answer is logged once per outage, not every
    interval: worth knowing, and not worth a line every fifteen seconds for
    an hour.
    """

    def __init__(self, fleet: Any, rollup: HourlyRollup, *, interval_s: int, timeout_s: float = 3.0):
        self._fleet = fleet
        self._rollup = rollup
        self._interval = interval_s
        self._timeout = timeout_s
        self._down: set[str] = set()

    def targets(self) -> list[tuple[str, str, str]]:
        """(server, model_name, metrics URL) for each self-hosted server with an address.

        A server is host:listen port, as everywhere else in llmao. The URL
        is its public address, the one LiteLLM uses.
        """
        out = []
        for dep in self._fleet.deployments:
            srv = dep.vllm
            if not dep.self_hosted or srv is None or not dep.api_base:
                continue
            root = dep.api_base.rstrip("/").removesuffix("/v1")
            out.append((f"{srv.host}:{srv.listen_port}", dep.model_name, f"{root}/metrics"))
        return out

    async def _read(self, client: httpx.AsyncClient, server: str, url: str) -> Sample | None:
        try:
            resp = await client.get(url, timeout=self._timeout)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            if server not in self._down:
                self._down.add(server)
                _LOGGER.warning("sampler: %s unreachable (%s)", server, type(e).__name__)
            return None
        if server in self._down:
            self._down.discard(server)
            _LOGGER.info("sampler: %s answering again", server)
        return parse_metrics(resp.text)

    async def sample_once(self, client: httpx.AsyncClient) -> None:
        """One reading of every box, all at once: twenty boxes in turn could
        outlast the interval."""
        targets = self.targets()
        readings = await asyncio.gather(*(self._read(client, server, url) for server, _model, url in targets))
        for (server, model_name, _url), sample in zip(targets, readings, strict=True):
            if sample is None:
                self._rollup.missed(server)
            else:
                self._rollup.record(server, model_name, sample)

    async def run(self) -> None:
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    await self.sample_once(client)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.exception("sampler: reading the boxes failed")
                await asyncio.sleep(self._interval)

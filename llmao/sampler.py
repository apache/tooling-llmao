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
from collections import Counter
from typing import TYPE_CHECKING, Any

import httpx

from llmao.vllm_client import fetch_metrics

if TYPE_CHECKING:
    from llmao.capacity import HourlyRollup
    from llmao.vllm_client import Sample

_LOGGER = logging.getLogger(__name__)


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

    def targets(self) -> list[tuple[Any, str, str, str]]:
        """(VllmServer, server, model_name, root URL) for each self-hosted server with an address.

        A server is host:listen port, as everywhere else in llmao. The root
        is its public address, the one LiteLLM uses.
        """
        out = []
        for dep in self._fleet.deployments:
            srv = dep.vllm
            if not dep.self_hosted or srv is None or not dep.api_base:
                continue
            root = dep.api_base.rstrip("/").removesuffix("/v1")
            out.append((srv, f"{srv.host}:{srv.listen_port}", dep.model_name, root))
        return out

    async def _read(self, client: httpx.AsyncClient, server: str, root: str) -> Sample | None:
        # The wait is bounded here, where the reading is asked for: a box
        # that takes longer than the sampler allows is one that did not answer.
        try:
            async with asyncio.timeout(self._timeout):
                sample = await fetch_metrics(client, root)
        except (httpx.HTTPError, TimeoutError) as e:
            if server not in self._down:
                self._down.add(server)
                _LOGGER.warning("sampler: %s unreachable (%s)", server, type(e).__name__)
            return None
        if server in self._down:
            self._down.discard(server)
            _LOGGER.info("sampler: %s answering again", server)
        return sample

    async def sample_once(self, client: httpx.AsyncClient) -> None:
        """One reading of every box, all at once: twenty boxes in turn could
        outlast the interval."""
        targets = self.targets()
        # A box's price is shared by the servers on it, so each server's
        # hour carries its share and adding rows up gives the real bill.
        # Every server on the box counts, reachable or not.
        per_box = Counter(srv.host for srv in self._fleet.servers)
        readings = await asyncio.gather(*(self._read(client, server, root) for _srv, server, _model, root in targets))
        for (srv, server, model_name, _root), sample in zip(targets, readings, strict=True):
            if sample is None:
                self._rollup.missed(server)
                continue
            share = srv.rental_usd_per_hour / per_box[srv.host] if srv.rental_usd_per_hour is not None else None
            self._rollup.record(server, model_name, sample, usd_per_hour=share)
            # The KV cache size, for the /models drill-down and the oversized
            # check. Fixed at engine start, but taken from every reading: a
            # restarted vLLM may have sized it differently.
            if sample.kv_cache_tokens is not None:
                srv.kv_cache_tokens = sample.kv_cache_tokens

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

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

"""Sample fleet contention, so the pilot answers a question.

WHY THIS EXISTS

The weekly token allowance is a proxy. What we actually want to know is
whether free-tier work and project work ever contend for a GPU -- because if
they rarely do, the allowance is solving a problem that is not there, and the
replacement design (defer a free-tier call only when projects need the box)
needs no allowance at all.

Nobody can answer that from the spend log: it records what ran, not what
waited. Queue depth is only visible while it is happening, so it has to be
sampled.

WHAT IT RECORDS

One line per box per interval, as structured JSON on the logger. Not a
database: a pilot is weeks, the volume is a few thousand lines, and grep and
jq answer every question we currently have. A table would be a schema to
design before knowing what the questions are.

    {"ts": ..., "box": "gemma4-26b", "running": 4, "waiting": 2,
     "kv_pct": 31.0, "preempted": 0}

The three that matter:

  waiting > 0 while running > 0   contention is real and measurable
  waiting == 0 throughout         the allowance is unnecessary; capacity is
                                  not the scarce thing
  preempted climbing              vLLM is evicting under memory pressure,
                                  which is a different problem from queueing
                                  and wants a different fix
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

_LOGGER = logging.getLogger("llmao.contention")

# Metric names, with the vllm: prefix stripped at parse time.
_WANTED = {
    "num_requests_running": "running",
    "num_requests_waiting": "waiting",
    "gpu_cache_usage_perc": "kv_pct",
    "num_preemptions": "preempted",
    "num_preemptions_total": "preempted",
}


@dataclass
class Sample:
    ts: float
    box: str
    running: float = 0.0
    waiting: float = 0.0
    kv_pct: float = 0.0
    preempted: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": round(self.ts, 1),
            "box": self.box,
            "running": self.running,
            "waiting": self.waiting,
            "kv_pct": round(self.kv_pct * 100, 1) if self.kv_pct <= 1 else round(self.kv_pct, 1),
            "preempted": self.preempted,
        }


def parse_metrics(box: str, text: str, *, now: float | None = None) -> Sample:
    """Pull the four figures out of a Prometheus exposition response.

    Hand-parsed rather than via a client library: four metrics, one format,
    and a dependency for this would be heavier than the code.
    """
    out = Sample(ts=now if now is not None else time.time(), box=box)
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition("{")
        if not rest:
            name, _, value = line.partition(" ")
        else:
            _, _, value = rest.partition("} ")
        field = _WANTED.get(name.removeprefix("vllm:"))
        if not field:
            continue
        try:
            setattr(out, field, float(value.strip()))
        except (TypeError, ValueError):
            continue
    return out


class ContentionSampler:
    """Polls each box's /metrics and logs a line per sample.

    Failures are logged once per box per outage rather than every interval:
    a box being down is worth knowing and is not worth a line every fifteen
    seconds for an hour.
    """

    def __init__(self, fleet: Any, *, interval_s: int = 15, timeout_s: float = 3.0):
        self._fleet = fleet
        self._interval = interval_s
        self._timeout = timeout_s
        self._down: set[str] = set()
        self._task: asyncio.Task | None = None

    def _targets(self) -> list[tuple[str, str]]:
        """(box name, metrics url) for every self-hosted deployment serving."""
        out = []
        for dep in getattr(self._fleet, "deployments", None) or []:
            if not getattr(dep, "self_hosted", False):
                continue
            base = getattr(dep, "api_base", None)
            if not base:
                continue
            root = base.rstrip("/")
            if root.endswith("/v1"):
                root = root[: -len("/v1")]
            out.append((getattr(dep, "model_name", getattr(dep, "name", "?")), f"{root}/metrics"))
        return out

    async def sample_once(self, client: httpx.AsyncClient) -> list[Sample]:
        samples = []
        for box, url in self._targets():
            try:
                resp = await client.get(url, timeout=self._timeout)
                resp.raise_for_status()
            except Exception as e:
                if box not in self._down:
                    self._down.add(box)
                    _LOGGER.warning("contention: %s unreachable (%s)", box, type(e).__name__)
                continue
            self._down.discard(box)
            samples.append(parse_metrics(box, resp.text))
        return samples

    async def run(self) -> None:
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    for s in await self.sample_once(client):
                        # One JSON object per line: greppable, jq-able, and
                        # loadable into pandas without a parser.
                        _LOGGER.info("contention %s", json.dumps(s.as_dict()))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.exception("contention: sample failed")
                await asyncio.sleep(self._interval)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.run(), name="contention-sampler")
        return self._task

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

"""Runner: once a day, delete what has outlived its keep in LiteLLM's memory store.

It knows nothing about any store. main.py hands it a list of
(name, function) pairs, one per kind of record, and that list is the one
place that says what gets pruned. Each function belongs to its store,
applies that store's own rule, and returns how many records it deleted;
retention settings are bound in when the list is built.

    Pruner([
        ("requests", backend.requests.prune),
        ("actions", functools.partial(actions.prune, keep_days=days)),
    ])

Each is run on its own, so one failing does not stop the others. It runs
at startup and then daily, so a deploy never pushes pruning a day away.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

DAY_S = 86400

PruneJob = tuple[str, Callable[[], Awaitable[int]]]


class Pruner:
    def __init__(self, jobs: list[PruneJob], *, every_s: float = DAY_S):
        self._jobs = list(jobs)
        self._every = every_s

    async def prune_all(self) -> dict[str, int | None]:
        """Run every job once. Records deleted per job; None where it failed."""
        done: dict[str, int | None] = {}
        for name, job in self._jobs:
            try:
                done[name] = gone = await job()
            except asyncio.CancelledError:
                raise
            except Exception:
                done[name] = None
                _LOGGER.exception("prune: %s failed", name)
                continue
            if gone:
                _LOGGER.info("prune: removed %d %s", gone, name)
        return done

    async def run(self) -> None:
        while True:
            await self.prune_all()
            await asyncio.sleep(self._every)

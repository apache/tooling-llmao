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

"""A short log of what site admins did, for /admin's Recent work.

Not an audit trail: it is kept for action_retention_days and then pruned,
and it holds only what no other record keeps. Request decisions live on the
request, a service key's creator on the key, a tier's last change beside
its limits. What is here is everything else an admin can do: restrict a
project, approve a box, flag one, add or stop tracking a deployment, revoke
a key.

One row per action in LiteLLM's memory store, keyed by when:

    llmao:action:2026-10-19T14:02:11:<random>

so a day is a key prefix, read one request per day.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import secrets
from typing import Any

from easydict import EasyDict as edict  # noqa: N813

KEY_PREFIX = "llmao:action:"


def retention_days(cfg: Any) -> int:
    """config.yaml action_retention_days. Required; call at startup."""
    if "action_retention_days" not in cfg:
        raise ValueError("config.yaml: action_retention_days is required")
    try:
        days = int(cfg.action_retention_days)
    except (TypeError, ValueError) as e:
        raise ValueError("config.yaml: action_retention_days must be a whole number of days") from e
    if days <= 0:
        raise ValueError("config.yaml: action_retention_days must be > 0")
    return days


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class ActionLog:
    """Handed the MemoryStore."""

    def __init__(self, memory: Any):
        self._memory = memory

    async def record(
        self, *, by: str, what: str, subject: str, sentence: str, at: dt.datetime | None = None, **detail: Any
    ) -> None:
        """Keep one action. `sentence` is what the admin was told it did."""
        at = at or _now()
        stamp = at.isoformat(timespec="seconds").removesuffix("+00:00")
        row = edict(at=stamp, by=by, what=what, subject=subject, sentence=sentence, **detail)
        # The stamp is to the second, and two actions can land in the same
        # one (two admins, or one handler recording twice). A short random
        # suffix keeps their keys apart, so the second never overwrites the
        # first; the time still leads, so keys sort and group by day.
        await self._memory.put(f"{KEY_PREFIX}{stamp}:{secrets.token_hex(3)}", json.dumps(row, sort_keys=True))

    async def _day(self, day: dt.date) -> list[edict]:
        return [edict(item) for item in await self._memory.find(f"{KEY_PREFIX}{day.isoformat()}T")]

    async def recent(self, days: int, *, today: dt.date | None = None) -> list[edict]:
        """Every action of the last `days` days, today included, newest first."""
        today = today or _now().date()
        found = await asyncio.gather(*(self._day(today - dt.timedelta(days=n)) for n in range(days)))
        rows = []
        for item in (item for day in found for item in day):
            try:
                rows.append(edict(json.loads(item.get("value") or "")))
            except (TypeError, ValueError):
                continue
        return sorted(rows, key=lambda r: r.get("at") or "", reverse=True)

    async def prune(self, keep_days: int, *, today: dt.date | None = None, sweep_days: int = 7) -> int:
        """Delete the actions of the `sweep_days` days just past retention.

        Run daily, each day is swept seven times before it slips out of
        reach, so a few days with llmao down still get cleared.
        """
        today = today or _now().date()
        oldest_kept = today - dt.timedelta(days=keep_days - 1)
        gone = 0
        for n in range(1, sweep_days + 1):
            for item in await self._day(oldest_kept - dt.timedelta(days=n)):
                if item.get("key"):
                    await self._memory.delete(item.key)
                    gone += 1
        return gone

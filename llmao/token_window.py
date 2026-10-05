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

"""A rolling token allowance, kept as daily buckets.

WHY NOT A CALENDAR RESET

A fixed weekly reset has a boundary, and the boundary is both unfair and
exploitable. Exhausting a share on Monday morning costs six and a half days;
doing exactly the same thing on Sunday night costs a few hours. And 1M at
23:00 Sunday plus 1M at 00:01 Monday is two allowances in two minutes,
entirely within the rules.

A rolling window has no instant at which the counter drops, so neither
problem exists.

WHY BUCKETS RATHER THAN A LIVE SUM

A true rolling window means asking "how much did this user spend in the last
168 hours" on every request -- an aggregate over the spend log, per call.
Seven daily buckets give the same properties for a few integers: sum the last
seven, drop the eighth. Granularity is a day rather than a second, which
nobody notices, and it makes "when do I get more?" answerable, which a live
sum does not.

WHAT THIS IS NOT

Not a daily entitlement. There is one allowance and the daily shape is
emergent: a committer can spend the whole week's share in an hour and then
wait, which is how people actually work. A daily cap would force the spend to
be spread whether or not the work is.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

# Keys are UTC dates. A local-midnight boundary would move with the server's
# timezone and make the same usage count differently after a DST change.
_DAY = "%Y-%m-%d"


def _today(now: _dt.datetime | None = None) -> _dt.date:
    return (now or _dt.datetime.now(_dt.UTC)).date()


@dataclass
class RollingAllowance:
    """One principal's allowance: a cap, a window, and daily buckets.

    `buckets` maps an ISO date to tokens spent that day. Days outside the
    window are dropped on every read, so the structure never grows beyond
    `window_days` entries.
    """

    cap: int
    window_days: int = 7
    buckets: dict[str, int] = field(default_factory=dict)

    def _live(self, now: _dt.datetime | None = None) -> dict[str, int]:
        first = _today(now) - _dt.timedelta(days=self.window_days - 1)
        return {
            day: n
            for day, n in self.buckets.items()
            if _dt.date.fromisoformat(day) >= first
        }

    def used(self, now: _dt.datetime | None = None) -> int:
        return sum(self._live(now).values())

    def remaining(self, now: _dt.datetime | None = None) -> int:
        return max(0, self.cap - self.used(now))

    def exhausted(self, now: _dt.datetime | None = None) -> bool:
        return self.remaining(now) <= 0

    def spend(self, tokens: int, now: _dt.datetime | None = None) -> None:
        """Record usage. Does not refuse -- the caller decides that.

        Recording happens after a call completes, so the final tokens of an
        allowance can overshoot it. That is deliberate: refusing mid-stream
        would mean a half-finished answer, and the overshoot is bounded by
        one request.
        """
        if tokens <= 0:
            return
        self.buckets = self._live(now)
        key = _today(now).strftime(_DAY)
        self.buckets[key] = self.buckets.get(key, 0) + int(tokens)

    def returns(self, now: _dt.datetime | None = None) -> list[tuple[_dt.date, int]]:
        """When tokens come back, oldest first.

        This is the question a fixed window answers trivially and a live
        rolling sum cannot answer at all: "I am out -- when can I work?"
        Each bucket ages out `window_days` after the day it was spent.
        """
        out = [
            (_dt.date.fromisoformat(day) + _dt.timedelta(days=self.window_days), n)
            for day, n in self._live(now).items()
        ]
        return sorted(out)

    def to_dict(self) -> dict[str, Any]:
        return {"cap": self.cap, "window_days": self.window_days,
                "buckets": dict(self.buckets)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None, *, cap: int,
                  window_days: int = 7) -> RollingAllowance:
        """Rehydrate, taking cap and window from CONFIG rather than storage.

        A stored cap would go stale the moment an admin changed the tier, and
        would quietly keep granting the old allowance.
        """
        raw = raw or {}
        return cls(cap=cap, window_days=window_days,
                   buckets=dict(raw.get("buckets") or {}))

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

"""Figures and moments as a person reads them at a glance.

Every page and log line that shows a number to a human formats it here,
so a token count, a sum of money or a time ago reads the same wherever it
appears. Nothing here computes: a function takes a value already worked
out and returns the words for it.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any


def money(amount: float) -> str:
    """$1,234.56"""
    return f"${amount:,.2f}"


def tokens(n: int) -> str:
    """Human token count. 340k reads faster than 340,000 and this is a glance."""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def gigabytes(n: int) -> str:
    """A byte count as whole gigabytes, the way a disk is sold: 98 GB."""
    return f"{n / 1_000_000_000:.0f} GB"


def pct(fraction: float) -> int:
    """A fraction as a whole percentage, held to 0..100."""
    return max(0, min(100, round(fraction * 100)))


def seconds(value) -> str:
    """2.5s under ten seconds, 15s above; a dash for none."""
    if value is None:
        return "—"
    return f"{value:.1f}s" if value < 10 else f"{value:.0f}s"


def day(hour: str) -> str:
    """'Oct 19' from an hour label or a day."""
    return dt.date.fromisoformat(hour[:10]).strftime("%b %-d")


def span(first_hour: str, last_hour: str) -> str:
    """'Oct 19 to Nov 2', or the one day."""
    first, last = day(first_hour), day(last_hour)
    return first if first == last else f"{first} to {last}"


def when(a_day: dt.date) -> str:
    """'tomorrow' / 'Tuesday' / a date, whichever is most useful.

    'Your tokens return 2026-09-23' is technically complete and makes the
    reader do arithmetic. The point of showing a return date at all is to turn
    a dead end into a plan.
    """
    today = dt.datetime.now(dt.UTC).date()
    delta = (a_day - today).days
    if delta <= 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta < 7:
        return f"on {a_day.strftime('%A')}"
    return f"on {a_day.isoformat()}"


def _stamp(stamp: str) -> dt.datetime | None:
    """An ISO stamp as an aware UTC moment; None if unreadable."""
    try:
        at = dt.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=dt.UTC)


def ago(stamp: str) -> str:
    """'5m ago', '3h ago', '2d ago' from an ISO stamp; '' if unreadable.

    Pair it with moment() in a title: the glance here, the exact time under
    the pointer.
    """
    at = _stamp(stamp)
    if at is None:
        return ""
    secs = max(0, int((dt.datetime.now(dt.UTC) - at).total_seconds()))
    if secs < 3600:
        return f"{max(1, secs // 60)}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def moment(stamp: str) -> str:
    """'2026-10-08 14:30 UTC' from an ISO stamp; '' if unreadable.

    The server clock is UTC and the label says so, rather than shifting to
    the reader's zone. A page shows ago() and keeps this in the title; the
    template names are <event>_ago and <event>_at.
    """
    at = _stamp(stamp)
    return at.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC") if at else ""


def ago_short(ts: float | None) -> str:
    """'5s', '3m', '2h' from a timestamp, for a log line or a tight cell; 'never' for none."""
    if ts is None:
        return "never"
    sec = max(0, int(time.time() - ts))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h"


def plural(n: int, noun: str) -> str:
    """'1 hour', '10 hours': a count with its noun, so a sentence never says '1 hours'."""
    return f"{n:,} {noun}" if n == 1 else f"{n:,} {noun}s"


# A log line is also something a person reads, which is why this lives here
# beside the rest of the rendering rather than in whichever module needed it
# first. Two modules need it, and two copies of a sanitiser is how they drift.
LOG_SAFE_LIMIT = 500


def log_safe(value: Any, limit: int = LOG_SAFE_LIMIT) -> str:
    """One line of someone else's text, safe to put in a log line.

    Anything that reaches a log through %s and came from outside the gateway can
    forge a line of its own: a newline in the middle of a value makes the
    rest of it look like the next entry. Escaping the three characters that
    do that -- CR, LF and NUL -- plus the ESC that would otherwise colour or
    reposition a terminal tailing the file, leaves the value readable and
    unable to pretend to be anything else.

    Truncation happens FIRST, so a cut cannot land between a backslash and
    the letter it escapes and leave a dangling one.
    """
    text = str(value)
    if len(text) > limit:
        text = text[:limit] + "…"
    return (
        text.replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\x00", "\\0")
        .replace("\x1b", "\\e")
    )

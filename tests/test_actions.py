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
"""The log of admin actions behind /admin's Recent work (llmao/actions.py)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.actions import ActionLog, retention_days
from llmao.proxy.client import MemoryStore
from tests.fake_transport import fake_memory

UTC = dt.UTC
DAY = dt.date(2026, 10, 19)


def _log():
    transport, rows = fake_memory()
    return ActionLog(MemoryStore(transport)), rows


def _at(day: dt.date, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC)


def test_an_action_is_kept_under_its_time():
    log, rows = _log()
    asyncio.run(
        log.record(
            by="admin1",
            what="restrict_project",
            subject="free-tier",
            sentence="Restricted free-tier to our own boxes.",
            at=_at(DAY, 14, 2),
        )
    )
    (key,) = rows
    assert key.startswith("llmao:action:2026-10-19T14:02:00:")
    assert json.loads(rows[key]) == {
        "at": "2026-10-19T14:02:00",
        "by": "admin1",
        "what": "restrict_project",
        "subject": "free-tier",
        "sentence": "Restricted free-tier to our own boxes.",
    }


def test_two_actions_in_one_second_are_both_kept():
    log, rows = _log()
    for subject in ("a", "b"):
        asyncio.run(log.record(by="x", what="w", subject=subject, sentence=subject, at=_at(DAY, 9)))
    assert len(rows) == 2


def test_recent_is_newest_first_over_the_days_asked_for():
    log, _ = _log()
    yesterday = DAY - dt.timedelta(days=1)
    for at, sentence in ((_at(yesterday, 9), "old"), (_at(DAY, 8), "early"), (_at(DAY, 17), "late")):
        asyncio.run(log.record(by="x", what="w", subject="s", sentence=sentence, at=at))
    assert [r.sentence for r in asyncio.run(log.recent(2, today=DAY))] == ["late", "early", "old"]
    assert [r.sentence for r in asyncio.run(log.recent(1, today=DAY))] == ["late", "early"]


def test_pruning_drops_only_the_days_past_retention():
    log, rows = _log()
    for day in (dt.date(2026, 7, 20), dt.date(2026, 7, 21), dt.date(2026, 7, 22)):
        asyncio.run(log.record(by="x", what="w", subject="s", sentence=str(day), at=_at(day, 12)))
    # Keeping 90 days on 2026-10-19 keeps 2026-07-22 onwards.
    assert asyncio.run(log.prune(90, today=DAY)) == 2
    assert [json.loads(v)["sentence"] for v in rows.values()] == ["2026-07-22"]


def test_retention_is_required():
    assert retention_days(edict(action_retention_days=90)) == 90
    with pytest.raises(ValueError, match="action_retention_days is required"):
        retention_days(edict())
    for bad in (0, -1, "ninety"):
        with pytest.raises(ValueError, match="action_retention_days must be"):
            retention_days(edict(action_retention_days=bad))

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
"""What /admin says about requests, approvals and recent work (pages.py)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import pathlib

import asfquart
from easydict import EasyDict as edict  # noqa: N813

from llmao.access_requests import KIND_CAPACITY, KIND_KEY, STATE_APPROVED, STATE_DENIED, AccessRequest
from llmao.proxy.client import BackendUnavailableError, KeyInfo

THIS_DIR = pathlib.Path(__file__).resolve().parent.parent


def _app():
    if asfquart.APP is None:
        asfquart.construct(
            "llmao", app_dir=str(THIS_DIR), cfg_file="config.yaml.example", oauth=False, force_login=False
        )
    import pages

    return pages


def _key(**kw):
    base = dict(
        kind=KIND_KEY,
        requester="johndoe",
        project=None,
        reason="nightly build",
        wanted={"purpose": "hadoop CI", "tier": "service"},
    )
    base.update(kw)
    return AccessRequest.new(**base)


def _capacity(**kw):
    base = dict(
        kind=KIND_CAPACITY, requester="alice", project="tooling", reason="audit", wanted={"token_cap": 40_000_000}
    )
    base.update(kw)
    return AccessRequest.new(**base)


def test_what_approving_does_is_said_plainly():
    pages = _app()
    assert pages._approval_means(_key()) == (
        'Lets johndoe create one service key, "hadoop CI". Nothing is created until they do; they have 14 days.'
    )
    free = _key(wanted={"purpose": "hadoop extension", "tier": "free_tier"})
    assert "one personal free-tier key" in pages._approval_means(free)
    assert pages._approval_means(_capacity()) == "Sets tooling's weekly allocation to 40M tokens, at once."


def test_an_approval_not_yet_used_is_listed_until_it_is_used():
    pages = _app()
    granted, used, pending = _key(), _key(requester="bob"), _key(requester="carol")
    for r in (granted, used):
        r.decide(state=STATE_APPROVED, by="admin1")
    used.consume()
    rows = pages._open_approval_rows([granted, used, pending, _capacity()])
    assert [r.requester for r in rows] == ["johndoe"]
    assert rows[0].ask == 'one service key, "hadoop CI"'
    assert rows[0].decided_by == "admin1"
    assert rows[0].expires_h


def test_a_decision_is_confirmed_in_words():
    pages = _app()
    r = _key()
    r.decide(state=STATE_APPROVED, by="admin1")
    assert pages._decided_h(r).startswith('Approved: johndoe may create one service key, "hadoop CI" until 20')
    c = _capacity()
    c.decide(state=STATE_APPROVED, by="admin1")
    assert pages._decided_h(c) == "Raised tooling's weekly allocation to 40M. It applies now."
    d = _key()
    d.decide(state=STATE_DENIED, by="admin1", reason="use a project key")
    assert pages._decided_h(d) == "Denied johndoe's request. They will see your reason."


class _Keys:
    def __init__(self, keys=(), down=False):
        self._keys, self._down = list(keys), down

    async def find(self, **_kw):
        if self._down:
            raise BackendUnavailableError("down")
        return self._keys


class _Tiers:
    def __init__(self, changes):
        self._changes = changes

    async def last_change(self, tier):
        return self._changes.get(tier)


class _Actions:
    def __init__(self, rows):
        self._rows = rows

    async def recent(self, _days):
        return self._rows


def _recent(monkeypatch, stored, *, keys=(), tiers=None, actions=(), keys_down=False, only=None):
    pages = _app()
    monkeypatch.setattr(
        pages.APP, "backend", edict(keys=_Keys(keys, keys_down), tiers=_Tiers(tiers or {})), raising=False
    )
    monkeypatch.setattr(pages.APP, "actions", _Actions(list(actions)), raising=False)
    return asyncio.run(pages._recent_work(stored, only=only))


def _stamp(hours_ago):
    return (dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def test_recent_work_merges_every_record_newest_first(monkeypatch):
    approved = _key()
    approved.decide(state=STATE_APPROVED, by="admin1")
    approved.decided_at = _stamp(5)
    approved.consume()
    approved.consumed_at = _stamp(1)
    service = KeyInfo(
        token_id="key-1",  # noqa: S106 -- an id, not a secret
        project="tooling",
        user=None,
        purpose="nightly",
        team_id="x",
        spend=0.0,
        max_budget=None,
        created_at=_stamp(3),
        last_used=None,
        created_by="admin2",
    )
    action = edict(
        at=_stamp(2), by="admin2", what="restrict_project", sentence="Restricted free-tier to our own boxes."
    )
    tier = {"free_tier": edict(by="admin1", at=_stamp(4))}
    work = _recent(monkeypatch, [approved], keys=[service], tiers=tier, actions=[action])
    assert [w.sentence for w in work] == [
        'johndoe used their approval and created one service key, "hadoop CI".',
        "Restricted free-tier to our own boxes.",
        'Created tooling\'s service key "nightly".',
        "Changed the free tier's limits.",
        'Approved johndoe to create one service key, "hadoop CI".',
    ]
    assert [w.by for w in work] == ["johndoe", "admin2", "admin2", "admin1", "admin1"]
    assert work[1].icon == "bi-shield-lock"


def test_recent_work_can_be_just_mine_and_leaves_out_the_old(monkeypatch):
    old, mine = _key(), _key(requester="bob")
    for r, by, ago in ((old, "admin1", 24 * 30), (mine, "admin2", 1)):
        r.decide(state=STATE_DENIED, by=by, reason="no")
        r.decided_at = _stamp(ago)
    assert [w.by for w in _recent(monkeypatch, [old, mine])] == ["admin2"]
    assert _recent(monkeypatch, [old, mine], only="admin1") == []


def test_one_source_down_thins_the_list_rather_than_failing_the_page(monkeypatch):
    r = _key()
    r.decide(state=STATE_DENIED, by="admin1", reason="no")
    assert len(_recent(monkeypatch, [r], keys_down=True)) == 1


def _model(**over):
    m = edict(
        model_name="qwen3-8b",
        cost_usd=10.0,
        last_hour="2026-11-02T10",
        cost_per_day=1.0,
        tokens_per_day=1000,
        usd_per_mtok=None,
        busy=0.85,
        waiting=0.0,
        kv_peak=0.2,
        waiting_peak=0,
        preemptions=0,
        queue_p95_worst_s=None,
        prompt_tokens=100,
        generation_tokens=10,
        requests=3,
        hours=2,
        unpriced_hours=0,
        daily_tokens=[],
        servers=[],
    )
    m.update(over)
    return m


def _demand(**over):
    d = edict(
        model="qwen3-8b",
        days=3,
        last_hour="2026-11-01T23",
        requests=30,
        requests_per_day=10.0,
        prompt_tokens=3000,
        completion_tokens=300,
        refused_total=0,
        refused=[],
        by_project=[],
        by_tool=[],
        prompt_len=[],
        duration_p50_s=0.3,
        duration_p95_worst_s=4.0,
        daily_requests=[],
    )
    d.update(over)
    return d


def test_one_card_per_model_live_first_and_the_forgotten_as_a_footnote():
    pages = _app()
    today = dt.date(2026, 11, 2)
    supply = [
        _model(model_name="cheap", cost_usd=1.0),
        _model(model_name="dear", cost_usd=50.0),
        # Nothing serves it now, and its last hour is twelve days back.
        _model(model_name="retired", cost_usd=99.0, last_hour="2026-10-21T18", hours=35),
        # Nothing serves it now either, but it was seen yesterday: still a card.
        _model(model_name="paused", cost_usd=2.0, last_hour="2026-11-01T18"),
    ]
    demand = [_demand(model="dear"), _demand(model="asked-only")]
    cards, dormant = pages.capacity_cards(
        supply, demand, catalog={"dear": "Dear Model"}, live={"cheap", "dear", "asked-only"}, today=today, period_days=7
    )
    # Live by cost, then the rest; a model from the demand rows alone is a card too.
    assert [c.model_name for c in cards] == ["dear", "cheap", "asked-only", "paused"]
    assert [bool(c.live) for c in cards] == [True, True, True, False]
    assert [c.display_name for c in cards] == ["Dear Model", "cheap", "asked-only", "paused"]
    dear, cheap, asked, _paused = cards
    assert dear.supply is not None and dear.demand is not None
    assert cheap.demand is None and asked.supply is None
    assert asked.last_seen == "Nov 1" and asked.cost == ""
    (gone,) = dormant
    assert (gone.model_name, gone.last_seen, gone.cost, gone.hours) == ("retired", "Oct 21", "$99.00", 35)


def test_the_worst_queue_time_reads_as_a_person_would_say_it():
    pages = _app()
    assert pages._capacity_model_row(_model()).queue_p95 == ""
    # vLLM's smallest bucket: it cannot tell 0.01s from 0.29s, and "under
    # 0.3s" is the same news as nothing, so it is left out.
    assert pages._capacity_model_row(_model(queue_p95_worst_s=0.3)).queue_p95 == ""
    assert pages._capacity_model_row(_model(queue_p95_worst_s=2.5)).queue_p95 == "2.5s"


def _line(**over):
    line = edict(
        server="Vast 1:8001",
        rental="Vast 1",
        label="RTX 3060 x2",
        addresses=["203.0.113.10:8001"],
        first_hour="2026-10-19T08",
        last_hour="2026-11-02T10",
        hours=300,
        usd_per_hour=None,
        busy=0,
        waiting=0,
        kv_peak=0,
        preemptions=0,
        tokens=0,
    )
    line.update(over)
    return line


def test_what_never_happened_is_not_shown():
    """A zero meter, a zero count and an empty column say nothing: each is left out."""
    pages = _app()
    quiet = pages._capacity_model_row(_model(servers=[_line()]))
    assert not quiet.queued and quiet.preemptions is None and not quiet.servers_queued
    assert not quiet.servers_preempted and quiet.servers[0].preempted == ""
    busy = pages._capacity_model_row(
        _model(
            waiting=0.01,
            waiting_peak=3,
            preemptions=60,
            servers=[_line(waiting=0.01, preemptions=60), _line(server="Vast 2:8001", rental="Vast 2")],
        )
    )
    assert busy.queued and busy.preemptions == 60 and busy.servers_queued
    # The column says which rental: the one that preempted has the count, the other nothing.
    assert busy.servers_preempted and [s.preempted for s in busy.servers] == [60, ""]
    # A peak with no share: a request queued once, briefly. Still told.
    assert pages._capacity_model_row(_model(waiting_peak=1)).queued


def test_a_server_line_says_when_it_was_seen_and_what_became_of_its_machine():
    pages = _app()
    lines = [
        _line(),
        _line(server="RunPod p:8001", rental="RunPod p", label="llmao-q"),
        _line(server="Vast 9:8001", rental="Vast 9", first_hour="2026-10-19T08", last_hour="2026-10-20T18"),
        _line(server="192.0.2.4:8001", rental="", label="", addresses=["192.0.2.4:8001"], last_hour="2026-10-19T23"),
        _line(server="192.0.2.5:8001", rental="", label="", addresses=["192.0.2.5:8001"]),
    ]
    listed = {"Vast 1": "running", "RunPod p": "exited"}
    row = pages._capacity_model_row(_model(servers=lines), listed=listed, live_servers={"192.0.2.5:8001"})
    assert [(s.server, s.label, s.seen, s.status) for s in row.servers] == [
        ("Vast 1:8001", "RTX 3060 x2", "Oct 19 to Nov 2", ""),
        ("RunPod p:8001", "llmao-q", "Oct 19 to Nov 2", "exited"),
        ("Vast 9:8001", "RTX 3060 x2", "Oct 19 to Oct 20", "ended"),
        ("192.0.2.4:8001", "", "Oct 19", "gone"),
        ("192.0.2.5:8001", "", "Oct 19 to Nov 2", ""),
    ]
    # No provider read yet: nothing can be said about a stamped line.
    row = pages._capacity_model_row(_model(servers=lines[:3]), listed=None)
    assert [s.status for s in row.servers] == ["", "", ""]


def test_a_busy_model_is_marked():
    pages = _app()
    row = pages._capacity_model_row(_model())
    assert row.busy == 85 and bool(row.busy_hot)


def test_a_day_series_is_charted_over_every_day_of_the_period_a_missing_day_as_zero():
    pages = _app()
    days = pages._period(dt.date(2026, 10, 20), 3)
    assert days == ["2026-10-18", "2026-10-19", "2026-10-20"]
    series = [edict(day="2026-10-18", value=50), edict(day="2026-10-20", value=200)]
    assert json.loads(pages._chart(series, days, "tokens")) == {
        "days": days,
        "values": [50, 0, 200],
        "unit": "tokens",
    }
    # Nothing recorded at all: no chart, rather than a row of zeros.
    assert pages._chart([], days, "tokens") == ""
    row = pages._capacity_model_row(_model(daily_tokens=series), days=days)
    assert json.loads(row.token_chart)["values"] == [50, 0, 200]

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

"""Reading usage from LiteLLM's daily aggregate, for three pages to group.

The arithmetic here is invisible when it is wrong: a page renders, the
numbers look plausible, and nobody can tell a dropped row from a quiet week.
"""

import asyncio
import datetime as dt

from llmao.fleet import Fleet, FleetDeployment
from llmao.intent import provider_intent
from llmao.proxy.client import PROJECT_FREE_TIER, UsageManager
from tests.fake_transport import FakeTransport

NOON = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.UTC)
TODAY = "2026-10-12"
YESTERDAY = "2026-10-11"

# project -> team id, as team/list would have filled it
TEAMS = {"tooling": "team-tooling", "mahout": "team-mahout", PROJECT_FREE_TIER: "team-free"}


def _entry(tokens=None, *, user=None, project=None, spend=0.0, requests=1, **metrics):
    """One key's figures for one model on one day, as LiteLLM nests them."""
    m = {"spend": spend, "api_requests": requests, **metrics}
    if tokens is not None:
        m["total_tokens"] = tokens
    return {
        "metrics": m,
        "metadata": {"user_id": user, "team_id": TEAMS.get(project), "key_alias": None},
    }


def _day(date, groups, models=None):
    """A day of the answer: {model group: {key: entry}}, and optionally {provider's model: {key: entry}}."""
    breakdown = {"model_groups": {g: {"api_key_breakdown": keys} for g, keys in groups.items()}}
    if models is not None:
        breakdown["models"] = {m: {"api_key_breakdown": keys} for m, keys in models.items()}
    return {"date": date, "breakdown": breakdown}


class _Teams:
    """Stands in for TeamManager: knows whose team is whose, and nothing else."""

    async def projects(self, team_ids):
        return {tid: project for project, tid in TEAMS.items()}

    async def team_id(self, project):
        return TEAMS[project]


class _Tiers:
    """Stands in for TierManager: one allowance, whatever the tier."""

    def __init__(self, window):
        self.window = window
        self.cap = 1_000_000

    async def limits(self, tier):
        return {"token_cap": self.cap, "token_window_days": self.window}


def _usage(days, *, now=NOON, page_size=1000, window=7):
    """A real UsageManager over a stand-in for LiteLLM.

    The stand-in honours the date range it is asked for, because which days
    get asked for is half of what is under test.
    """

    def answer(call):
        p = call.params
        hits = [
            d
            for d in days
            # something that is not a day at all is passed through, to be ignored
            if not isinstance(d, dict) or p.start_date <= d["date"] <= p.end_date
        ]
        first = (p.page - 1) * p.page_size
        return {
            "results": hits[first : first + p.page_size],
            "metadata": {"has_more": first + p.page_size < len(hits)},
        }

    # gemma runs on our own box; claude-opus-5 is bought in
    fleet = Fleet(
        None,
        [],
        deployments=[
            FleetDeployment(model_name="gemma4-26b", name="gemma4-26b", self_hosted=True),
            FleetDeployment(model_name="claude-opus-5", name="claude-opus-5", self_hosted=False),
        ],
    )
    b = UsageManager(FakeTransport(answer), fleet, _Teams(), _Tiers(window))
    b._DAILY_PAGE_SIZE = page_size
    b._now = lambda: now
    return b


def _asked(b):
    """What LiteLLM was asked for, as (path, params)."""
    return [(c.path, c.params) for c in b._transport.calls]


def _a_minute_later(b):
    """The open days have aged out, so the next read asks LiteLLM again."""
    b._daily_at = None


def _run(coro):
    return asyncio.run(coro)


WEEK = [
    _day(
        TODAY,
        {
            "gemma4-26b": {
                # one person, one model, one key, two days -- must fold to a single row
                "tok1": _entry(100, user="alice", project="tooling", requests=1),
                # same person, different key -- must NOT fold together
                "tok2": _entry(7, user="alice", project="tooling"),
                # someone else
                "tok4": _entry(900, user="bob", project="tooling"),
            },
            # a commercial row: real money, no cost_basis tag
            "claude-opus-5": {"tok1": _entry(40, user="alice", project="tooling", spend=1.25)},
        },
    ),
    _day(
        YESTERDAY,
        {
            "gemma4-26b": {
                "tok1": _entry(50, user="alice", project="tooling", requests=1),
                # different project
                "tok3": _entry(300, user="alice", project="mahout"),
            },
        },
    ),
]


def test_rows_fold_by_user_project_model_and_key():
    rows = _run(_usage(WEEK)._spend_rows())
    # six entries over two days, five distinct (user, project, model, key) combinations
    assert len(rows) == 5
    folded = [r for r in rows if r["token_id"] == "tok1" and r["model"] == "gemma4-26b"]
    assert len(folded) == 1
    assert folded[0]["total_tokens"] == 150
    assert folded[0]["requests"] == 2
    assert folded[0]["project"] == "tooling"


def test_prompt_and_completion_are_summed_when_total_is_absent():
    """LiteLLM does not always send total_tokens."""
    days = [
        _day(
            TODAY,
            {
                "gemma4-26b": {
                    "t": _entry(user="alice", project="tooling", prompt_tokens=40000, completion_tokens=11000),
                }
            },
        )
    ]
    rows = _run(_usage(days)._spend_rows())
    assert rows[0]["total_tokens"] == 51000


def test_usage_filters_by_project():
    b = _usage(WEEK)
    tooling = _run(b.rows("tooling"))
    assert {r["project"] for r in tooling} == {"tooling"}
    assert sum(r["total_tokens"] for r in tooling) == 150 + 7 + 900 + 40


def test_usage_with_none_returns_every_project():
    b = _usage(WEEK)
    assert {r["project"] for r in _run(b.rows(None))} == {"tooling", "mahout"}


def test_key_usage_groups_by_key_for_one_person():
    b = _usage(WEEK)
    got = _run(b.by_key("alice"))
    assert got == {"tok1": 150 + 40, "tok2": 7, "tok3": 300}
    assert "tok4" not in got, "another person's key must not appear"


def test_cost_basis_distinguishes_capacity_from_money():
    """The whole reason the tag is carried through.

    A self-hosted row's cost is derived from throughput at an assumed
    utilisation; a commercial row's is an amount payable. A page that sums
    them without checking produces a figure that means nothing.
    """
    rows = _run(_usage(WEEK)._spend_rows())
    selfhosted = [r for r in rows if r["model"] == "gemma4-26b"]
    commercial = [r for r in rows if r["model"] == "claude-opus-5"]
    assert all(r["cost_basis"] == "derived_capacity" for r in selfhosted)
    assert all(r["cost_basis"] is None for r in commercial)
    real_money = sum(r["total_cost_usd"] for r in rows if r["cost_basis"] != "derived_capacity")
    assert real_money == 1.25


def test_a_model_served_both_ways_is_not_called_an_estimate():
    """The same name can be on our boxes and bought in.

    Usage is recorded per name, so the two cannot be told apart, and some of
    those dollars are a bill. Calling the lot an estimate would hide it.
    """
    ours = FleetDeployment(model_name="m", name="m", self_hosted=True)
    bought = FleetDeployment(model_name="m", name="m-bedrock", self_hosted=False)
    assert Fleet(None, [], deployments=[ours]).cost_basis("m") == "derived_capacity"
    assert Fleet(None, [], deployments=[ours, bought]).cost_basis("m") is None
    assert Fleet(None, [], deployments=[bought]).cost_basis("m") is None
    assert Fleet(None, [], deployments=[]).cost_basis("never-heard-of-it") is None


def test_a_key_llmao_did_not_issue_is_kept():
    """No owner, no team -- but it still consumed the fleet.

    Dropping it would make the project figures add up while the fleet total
    quietly disagreed, which is the harder bug to find.
    """
    rows = _run(_usage([_day(TODAY, {"gemma4-26b": {"stray": _entry(500)}})])._spend_rows())
    assert len(rows) == 1
    assert rows[0]["total_tokens"] == 500
    assert rows[0]["project"] is None
    assert rows[0]["user"] is None


def test_second_read_is_cached():
    """A page refresh must not cost another read."""
    b = _usage(WEEK)
    _run(b._spend_rows())
    _run(b._spend_rows())
    assert len(_asked(b)) == 1
    _a_minute_later(b)
    _run(b._spend_rows())
    assert len(_asked(b)) == 2


def test_malformed_entries_do_not_abort_the_read():
    """One bad entry must not cost the whole page."""
    days = [
        _day(
            TODAY,
            {
                "m": {"t": _entry("not a number", user="alice", project="tooling")},
                "m2": {"t2": _entry(10, user="alice", project="tooling"), "t3": "a string where a dict should be"},
                "m3": "a string where a group should be",
            },
        ),
        "a string where a day should be",
    ]
    rows = _run(_usage(days)._spend_rows())
    assert [r["total_tokens"] for r in rows] == [10]


def test_every_page_is_read():
    """A busy week must not be silently cut at the first page."""
    days = [
        _day(f"2026-10-{d:02d}", {"gemma4-26b": {"t": _entry(1, user="alice", project="tooling")}})
        for d in (10, 11, 12)
    ]
    b = _usage(days, page_size=2)
    rows = _run(b._spend_rows())
    assert rows[0]["total_tokens"] == 3
    assert [p.page for _, p in _asked(b)] == [1, 2]


# ---------------------------------------------------------------------------
# Which days are asked for
#
# /spend/logs ignored a start_date sent on its own and returned the last
# 10,000 requests, so "this week" was whatever that happened to span. Here
# both ends are always sent, and a day that has ended is read once.
# ---------------------------------------------------------------------------


def test_the_first_read_asks_for_exactly_the_week():
    b = _usage(WEEK)
    _run(b._spend_rows())
    assert _asked(b) == [
        ("user/daily/activity", {"start_date": "2026-10-06", "end_date": TODAY, "page": 1, "page_size": 1000})
    ]


def test_a_closed_day_is_not_read_again():
    """History does not change. Only today is worth asking about twice."""
    b = _usage(WEEK)
    _run(b._spend_rows())
    _a_minute_later(b)
    rows = _run(b._spend_rows())
    assert _asked(b)[-1][1].start_date == TODAY
    # ...and yesterday's figures are still in the answer
    assert sum(r["total_tokens"] for r in rows) == 100 + 7 + 900 + 40 + 50 + 300


def test_yesterday_stays_open_for_an_hour_after_midnight():
    """A request begun at 23:59 is filed under yesterday when it finishes."""
    b = _usage(WEEK, now=dt.datetime(2026, 10, 12, 0, 30, tzinfo=dt.UTC))
    _run(b._spend_rows())
    _a_minute_later(b)
    _run(b._spend_rows())
    assert _asked(b)[-1][1].start_date == YESTERDAY

    b._now = lambda: dt.datetime(2026, 10, 12, 1, 30, tzinfo=dt.UTC)
    _a_minute_later(b)
    _run(b._spend_rows())  # read once more after the hour, which closes it
    _a_minute_later(b)
    _run(b._spend_rows())
    assert _asked(b)[-1][1].start_date == TODAY


def test_a_day_that_leaves_the_window_is_dropped():
    b = _usage(WEEK)
    _run(b._spend_rows())
    b._now = lambda: NOON + dt.timedelta(days=7)
    rows = _run(b._spend_rows())
    assert rows == []
    assert min(b._daily) == "2026-10-13"


# ---------------------------------------------------------------------------
# The allowance is the same rows, one person's share, by day
# ---------------------------------------------------------------------------


# alice has a free-tier key as well as the project keys in WEEK, and so does bob
FREE = [
    _day(TODAY, {"gemma4-26b": {"free1": _entry(60, user="alice", project=PROJECT_FREE_TIER)}}),
    _day(
        YESTERDAY,
        {
            "gemma4-26b": {
                "free1": _entry(25, user="alice", project=PROJECT_FREE_TIER),
                "free2": _entry(999, user="bob", project=PROJECT_FREE_TIER),
            }
        },
    ),
]


def test_allowance_buckets_are_one_persons_free_tier_tokens_by_day():
    a = _run(_usage(FREE).allowance("alice"))
    assert a.buckets == {TODAY: 60, YESTERDAY: 25}
    assert a.cap == 1_000_000


def test_what_a_person_draws_through_a_project_is_not_their_allowance():
    """A project's key draws on the project's share.

    Counting it against the person as well filled their bar with work the
    allowance was never meant to cover.
    """
    a = _run(_usage(WEEK).allowance("alice"))
    assert a.buckets == {}
    assert a.used() == 0


def test_allowance_costs_no_read_of_its_own():
    """It was a second scan of the log, per person. Now it is the same rows."""
    b = _usage(WEEK)
    _run(b._spend_rows())
    _run(b.allowance("alice"))
    _run(b.allowance("bob"))
    assert len(_asked(b)) == 1


def test_a_changed_cap_shows_at_once_and_costs_no_read():
    """The cap is asked for each time. Changing it does not change what was used."""
    b = _usage(FREE)
    before = _run(b.allowance("alice"))
    b.tiers.cap = 500_000
    after = _run(b.allowance("alice"))
    assert (before.cap, after.cap) == (1_000_000, 500_000)
    assert after.buckets == before.buckets
    assert len(_asked(b)) == 1


def test_a_longer_allowance_window_reads_the_extra_days():
    b = _usage(WEEK, window=10)
    _run(b._spend_rows())
    _run(b.allowance("alice"))
    assert _asked(b)[-1][1].start_date == "2026-10-03"
    assert len(_asked(b)) == 2


def _bought_usage(days):
    """A UsageManager whose fleet buys grok-4 from two providers."""
    b = _usage(days)
    b.fleet = Fleet(
        None,
        [],
        deployments=[
            FleetDeployment.from_intended(provider_intent("xai", "grok-4", "xai/grok-4", "https://api.x.ai/v1")),
            FleetDeployment.from_intended(
                provider_intent("openrouter", "grok-4", "openrouter/x-ai/grok-4", "https://openrouter.ai/api/v1")
            ),
        ],
    )
    return b


def test_provider_spend_tells_one_provider_from_another_by_their_model_string():
    """Both serve grok-4; only the provider's own model string says which was paid."""
    days = [
        _day(
            TODAY,
            {
                "grok-4": {
                    "k1": _entry(100, project="tooling", spend=1.5),
                    "k2": _entry(50, project="mahout", spend=0.25),
                }
            },
            models={
                "xai/grok-4": {"k1": _entry(100, project="tooling", spend=1.5)},
                "openrouter/x-ai/grok-4": {"k2": _entry(50, project="mahout", spend=0.25)},
                # our own box: not a provider we pay
                "hosted_vllm/gemma4-26b": {"k3": _entry(900, project="tooling")},
            },
        ),
        _day(YESTERDAY, {}, models={"xai/grok-4": {"k1": _entry(10, project="tooling", spend=0.5)}}),
    ]
    rows = asyncio.run(_bought_usage(days).provider_spend(2))
    got = sorted((r.day, r.provider, r.model_name, r.project, r.spend_usd, r.tokens) for r in rows)
    assert got == [
        (YESTERDAY, "xai", "grok-4", "tooling", 0.5, 10),
        (TODAY, "openrouter", "grok-4", "mahout", 0.25, 50),
        (TODAY, "xai", "grok-4", "tooling", 1.5, 100),
    ]


def test_with_no_provider_configured_there_is_no_spend_to_read():
    calls = []
    b = _usage([])
    b._transport = type("T", (), {"request": lambda *a, **k: calls.append(a)})()
    assert asyncio.run(b.provider_spend(30)) == []
    assert calls == []

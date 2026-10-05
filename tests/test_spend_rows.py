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

"""Folding the spend log into rows three pages can group.

The arithmetic here is invisible when it is wrong: a page renders, the
numbers look plausible, and nobody can tell a dropped row from a quiet week.
"""

import asyncio
import types

from llmao.litellm_client import LiteLLMBackend


def _backend(raw_rows):
    """A backend whose only live part is the fold.

    Constructed without __init__ so the test needs no config, no fleet and no
    HTTP client -- the thing under test is pure data shaping.
    """
    b = object.__new__(LiteLLMBackend)
    b._spend_cache = None
    b._cost_basis = {"gemma4-26b": "derived_capacity"}
    b._SPEND_TTL_S = 60

    async def fake_request(method, path, **kw):
        return types.SimpleNamespace(
            status_code=200,
            content=b"x",
            json=lambda: raw_rows,
        )

    b._request = fake_request
    b._raise_http = lambda resp: None
    return b


def _run(coro):
    return asyncio.run(coro)


LOG = [
    # one person, one model, two calls -- must fold to a single row
    {
        "user": "akm",
        "model": "gemma4-26b",
        "api_key": "tok1",
        "spend": 0.0,
        "total_tokens": 100,
        "metadata": {"project": "tooling"},
    },
    {
        "user": "akm",
        "model": "gemma4-26b",
        "api_key": "tok1",
        "spend": 0.0,
        "total_tokens": 50,
        "metadata": {"project": "tooling"},
    },
    # same person, different key -- must NOT fold together
    {
        "user": "akm",
        "model": "gemma4-26b",
        "api_key": "tok2",
        "spend": 0.0,
        "total_tokens": 7,
        "metadata": {"project": "tooling"},
    },
    # different project
    {
        "user": "akm",
        "model": "gemma4-26b",
        "api_key": "tok3",
        "spend": 0.0,
        "total_tokens": 300,
        "metadata": {"project": "mahout"},
    },
    # someone else
    {
        "user": "gstein",
        "model": "gemma4-26b",
        "api_key": "tok4",
        "spend": 0.0,
        "total_tokens": 900,
        "metadata": {"project": "tooling"},
    },
    # a commercial row: real money, no cost_basis tag
    {
        "user": "akm",
        "model": "claude-opus-5",
        "api_key": "tok1",
        "spend": 1.25,
        "total_tokens": 40,
        "metadata": {"project": "tooling"},
    },
]


def test_rows_fold_by_user_project_model_and_key():
    rows = _run(_backend(LOG)._spend_rows())
    # six log lines, five distinct (user, project, model, key) combinations
    assert len(rows) == 5
    folded = [r for r in rows if r["token_id"] == "tok1" and r["model"] == "gemma4-26b"]
    assert len(folded) == 1
    assert folded[0]["total_tokens"] == 150
    assert folded[0]["requests"] == 2


def test_prompt_and_completion_are_summed_when_total_is_absent():
    """LiteLLM does not always send total_tokens."""
    rows = _run(
        _backend(
            [
                {
                    "user": "akm",
                    "model": "gemma4-26b",
                    "api_key": "t",
                    "spend": 0.0,
                    "prompt_tokens": 40000,
                    "completion_tokens": 11000,
                    "metadata": {"project": "tooling"},
                },
            ]
        )._spend_rows()
    )
    assert rows[0]["total_tokens"] == 51000


def test_usage_filters_by_project():
    b = _backend(LOG)
    tooling = _run(b.usage("tooling"))
    assert {r["project"] for r in tooling} == {"tooling"}
    assert sum(r["total_tokens"] for r in tooling) == 150 + 7 + 900 + 40


def test_usage_with_none_returns_every_project():
    b = _backend(LOG)
    assert {r["project"] for r in _run(b.usage(None))} == {"tooling", "mahout"}


def test_key_usage_groups_by_key_for_one_person():
    b = _backend(LOG)
    got = _run(b.key_usage("akm"))
    assert got == {"tok1": 150 + 40, "tok2": 7, "tok3": 300}
    assert "tok4" not in got, "another person's key must not appear"


def test_cost_basis_distinguishes_capacity_from_money():
    """The whole reason the tag is carried through.

    A self-hosted row's cost is derived from throughput at an assumed
    utilisation; a commercial row's is an amount payable. A page that sums
    them without checking produces a figure that means nothing.
    """
    rows = _run(_backend(LOG)._spend_rows())
    selfhosted = [r for r in rows if r["model"] == "gemma4-26b"]
    commercial = [r for r in rows if r["model"] == "claude-opus-5"]
    assert all(r["cost_basis"] == "derived_capacity" for r in selfhosted)
    assert all(r["cost_basis"] is None for r in commercial)
    real_money = sum(r["total_cost_usd"] for r in rows if r["cost_basis"] != "derived_capacity")
    assert real_money == 1.25


def test_a_row_llmao_did_not_issue_is_kept():
    """No project, no user metadata -- but it still consumed the fleet.

    Dropping it would make the project figures add up while the fleet total
    quietly disagreed, which is the harder bug to find.
    """
    rows = _run(
        _backend(
            [
                {"model": "gemma4-26b", "api_key": "stray", "spend": 0.0, "total_tokens": 500},
            ]
        )._spend_rows()
    )
    assert len(rows) == 1
    assert rows[0]["total_tokens"] == 500
    assert rows[0]["project"] is None


def test_second_read_is_cached():
    """A page refresh must not cost another scan."""
    calls = []
    b = _backend(LOG)
    inner = b._request

    async def counting(*a, **kw):
        calls.append(1)
        return await inner(*a, **kw)

    b._request = counting
    _run(b._spend_rows())
    _run(b._spend_rows())
    assert len(calls) == 1
    b.invalidate_spend()
    _run(b._spend_rows())
    assert len(calls) == 2


def test_malformed_rows_do_not_abort_the_fold():
    """One bad row must not cost the whole page."""
    rows = _run(
        _backend(
            [
                {
                    "user": "akm",
                    "model": "m",
                    "api_key": "t",
                    "total_tokens": "not a number",
                    "metadata": {"project": "p"},
                },
                {"user": "akm", "model": "m2", "api_key": "t2", "total_tokens": 10, "metadata": {"project": "p"}},
                "a string where a dict should be",
            ]
        )._spend_rows()
    )
    assert any(r["total_tokens"] == 10 for r in rows)


# ---------------------------------------------------------------------------
# Token-cap backfill
#
# Projects come from LDAP, so nobody sets one up and nobody can be asked for
# its allocation. Teams predating the allocation have none, and more appear
# whenever a PMC is created while an older build runs.
# ---------------------------------------------------------------------------


def _backfill_backend(default_cap=20_000_000):
    b = object.__new__(LiteLLMBackend)
    b._cfg = types.SimpleNamespace(budgets=types.SimpleNamespace(default_team_token_cap=default_cap))
    b.sent = []

    async def fake_request(method, path, json=None, **kw):
        b.sent.append((path, json))
        return types.SimpleNamespace(status_code=200, content=b"{}", json=lambda: {})

    b._request = fake_request
    b._raise_http = lambda resp: None
    return b


def test_backfill_sets_a_cap_on_teams_without_one():
    b = _backfill_backend()
    _run(b._backfill_token_caps([{"team_alias": "tooling", "team_id": "t1", "metadata": {"grantor": "Free Tier"}}]))
    assert len(b.sent) == 1
    path, body = b.sent[0]
    assert path == "team/update"
    assert body["metadata"]["token_cap"] == 20_000_000
    # merged, not replaced -- grantor has to survive
    assert body["metadata"]["grantor"] == "Free Tier"


def test_backfill_leaves_an_existing_cap_alone():
    """Including one deliberately set to something unusual.

    The test is on the key being absent, not on its value, so a project
    capped at zero on purpose is not quietly reset to the default.
    """
    b = _backfill_backend()
    _run(
        b._backfill_token_caps(
            [
                {"team_alias": "tooling", "team_id": "t1", "metadata": {"token_cap": 5}},
                {"team_alias": "comdev", "team_id": "t2", "metadata": {"token_cap": 0}},
            ]
        )
    )
    assert b.sent == []


def test_backfill_is_idempotent_across_restarts():
    """It runs at every startup; the second boot must be a no-op."""
    b = _backfill_backend()
    rows = [{"team_alias": "tooling", "team_id": "t1", "metadata": {}}]
    _run(b._backfill_token_caps(rows))
    assert len(b.sent) == 1
    rows[0]["metadata"] = b.sent[0][1]["metadata"]
    _run(b._backfill_token_caps(rows))
    assert len(b.sent) == 1


def test_backfill_does_nothing_without_a_configured_default():
    b = _backfill_backend(default_cap=0)
    _run(b._backfill_token_caps([{"team_alias": "t", "team_id": "t1", "metadata": {}}]))
    assert b.sent == []


def test_backfill_skips_malformed_rows_without_stopping():
    """One bad row must not cost every other project its allocation."""
    b = _backfill_backend()
    _run(
        b._backfill_token_caps(
            [
                "not a dict",
                {"team_alias": None, "team_id": "t0", "metadata": {}},
                {"team_alias": "ok", "team_id": "t1", "metadata": {}},
            ]
        )
    )
    assert len(b.sent) == 1
    assert b.sent[0][1]["team_id"] == "t1"

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
"""TeamManager: LiteLLM's teams, one per project."""

import asyncio

import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.proxy.client import (
    PRETEND_PROJECTS,
    PROJECT_FREE_TIER,
    PROJECT_SERVICE,
    BackendUnavailableError,
    TeamManager,
)
from tests.fake_transport import FakeTransport


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Which team is whose
# ---------------------------------------------------------------------------


def test_a_team_not_yet_listed_is_looked_up():
    """A project created since the last listing must not show as no project."""
    listing = {"teams": [{"team_alias": "tooling", "team_id": "team-tooling"}]}
    teams = TeamManager(edict(), FakeTransport(lambda call: listing))
    assert _run(teams.projects({"team-tooling"})) == {"team-tooling": "tooling"}
    # ...and once it is known, it is not asked about again
    _run(teams.projects({"team-tooling"}))
    assert [c.path for c in teams._transport.calls] == ["team/list"]


# ---------------------------------------------------------------------------
# Making a team
# ---------------------------------------------------------------------------


def _new_team(project):
    """What LiteLLM is sent to create `project`'s team."""

    def litellm(call):
        return {"teams": []} if call.path == "team/list" else {"team_id": "t1", **(call.json or {})}

    cfg = edict(budgets=edict(default_team_budget_usd=100, duration="30d", default_team_token_cap=20_000_000))
    teams = TeamManager(cfg, FakeTransport(litellm))
    _run(teams.ensure(project))
    return next(c.json for c in teams._transport.calls if c.path == "team/new")


def test_a_real_projects_team_starts_with_a_dollar_budget():
    sent = _new_team("airflow")
    assert sent.max_budget == 100
    assert sent.budget_duration == "30d"


def test_a_pretend_projects_team_has_no_dollar_budget():
    """It would be one budget shared by everybody with a projectless key."""
    sent = _new_team(PROJECT_FREE_TIER)
    assert sent.team_alias == PROJECT_FREE_TIER
    assert "max_budget" not in sent
    assert "budget_duration" not in sent


def _litellm_with(existing):
    """A stand-in LiteLLM that has `existing` teams and can be given more."""
    if not isinstance(existing, dict):
        existing = {alias: [GROUP] if alias in PRETEND_PROJECTS else [] for alias in existing}
    teams = [{"team_alias": alias, "team_id": f"id-{alias}", "models": models} for alias, models in existing.items()]

    def litellm(call):
        if call.path == "team/new":
            new = call.json
            teams.append({"team_alias": new.team_alias, "team_id": f"id-{new.team_alias}", "models": new.get("models")})
            return teams[-1]
        if call.path == "team/update":
            next(t for t in teams if t["team_id"] == call.json.team_id)["models"] = call.json.models
            return {}
        if call.path == "team/info":
            return {"team_info": next(t for t in teams if t["team_id"] == call.params.team_id)}
        return {"teams": teams}

    cfg = edict(budgets=edict(default_team_budget_usd=100, duration="30d", default_team_token_cap=0))
    return TeamManager(cfg, FakeTransport(litellm))


def _made(teams):
    return [c.json.team_alias for c in teams._transport.calls if c.path == "team/new"]


def test_the_pretend_projects_are_made_at_startup():
    """So they always exist, and always have a team id to count usage by."""
    teams = _litellm_with(["airflow"])
    _run(teams.ensure_pretend_projects())
    assert _made(teams) == [PROJECT_FREE_TIER, PROJECT_SERVICE]
    assert _run(teams.team_id(PROJECT_FREE_TIER)) == f"id-{PROJECT_FREE_TIER}"
    assert _run(teams.team_id(PROJECT_SERVICE)) == f"id-{PROJECT_SERVICE}"


def test_pretend_projects_already_there_are_left_alone():
    teams = _litellm_with(["airflow", PROJECT_FREE_TIER, PROJECT_SERVICE])
    _run(teams.rows())
    _run(teams.ensure_pretend_projects())
    assert _made(teams) == []
    assert _updated(teams) == []


# ---------------------------------------------------------------------------
# What a pretend project's keys may use
#
# Our own boxes and nothing else: a key with no project has no PMC to be
# asked about a bill. LiteLLM enforces it, from the team's list of models.
# ---------------------------------------------------------------------------

GROUP = "llmao:self-hosted"


def _updated(teams):
    return [(c.json.team_id, c.json.models) for c in teams._transport.calls if c.path == "team/update"]


def test_a_pretend_project_is_made_restricted_to_our_own_boxes():
    teams = _litellm_with(["airflow"])
    _run(teams.ensure_pretend_projects())
    sent = [c.json for c in teams._transport.calls if c.path == "team/new"]
    assert [new.models for new in sent] == [[GROUP], [GROUP]]


def test_a_real_projects_team_is_not_restricted():
    """No list at all, which LiteLLM reads as any model."""
    assert "models" not in _new_team("airflow")


def test_a_pretend_project_found_unrestricted_is_put_right_and_reported(caplog):
    """Made before the restriction, or edited in LiteLLM. Either way a free key could reach a bill."""
    teams = _litellm_with({PROJECT_FREE_TIER: [], PROJECT_SERVICE: [GROUP, "claude-opus-5"]})
    with caplog.at_level("ERROR"):
        _run(teams.ensure_pretend_projects())
    assert _updated(teams) == [(f"id-{PROJECT_FREE_TIER}", [GROUP]), (f"id-{PROJECT_SERVICE}", [GROUP])]
    assert [r.levelname for r in caplog.records] == ["ERROR", "ERROR"]
    assert "any model" in caplog.records[0].getMessage()
    assert "claude-opus-5" in caplog.records[1].getMessage()


def test_the_order_litellm_lists_models_in_does_not_matter():
    assert TeamManager._PRETEND_MODELS == {GROUP}
    teams = _litellm_with({PROJECT_FREE_TIER: [GROUP], PROJECT_SERVICE: [GROUP]})
    _run(teams.ensure_pretend_projects())
    assert _updated(teams) == []


# ---------------------------------------------------------------------------
# Token-cap backfill
#
# Projects come from LDAP, so nobody sets one up and nobody can be asked for
# its allocation. Teams predating the allocation have none, and more appear
# whenever a PMC is created while an older build runs.
# ---------------------------------------------------------------------------


def _backfill_backend(default_cap=20_000_000):
    cfg = edict(budgets=edict(default_team_token_cap=default_cap))
    return TeamManager(cfg, FakeTransport())


def _sent(b):
    """What LiteLLM was sent, as (path, json)."""
    return [(c.path, c.json) for c in b._transport.calls]


def test_backfill_sets_a_cap_on_teams_without_one():
    b = _backfill_backend()
    _run(b.backfill_token_caps([{"team_alias": "tooling", "team_id": "t1", "metadata": {"grantor": "Free Tier"}}]))
    assert len(_sent(b)) == 1
    path, body = _sent(b)[0]
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
        b.backfill_token_caps(
            [
                {"team_alias": "tooling", "team_id": "t1", "metadata": {"token_cap": 5}},
                {"team_alias": "comdev", "team_id": "t2", "metadata": {"token_cap": 0}},
            ]
        )
    )
    assert _sent(b) == []


def test_backfill_is_idempotent_across_restarts():
    """It runs at every startup; the second boot must be a no-op."""
    b = _backfill_backend()
    rows = [{"team_alias": "tooling", "team_id": "t1", "metadata": {}}]
    _run(b.backfill_token_caps(rows))
    assert len(_sent(b)) == 1
    rows[0]["metadata"] = _sent(b)[0][1]["metadata"]
    _run(b.backfill_token_caps(rows))
    assert len(_sent(b)) == 1


def test_backfill_does_nothing_without_a_configured_default():
    b = _backfill_backend(default_cap=0)
    _run(b.backfill_token_caps([{"team_alias": "t", "team_id": "t1", "metadata": {}}]))
    assert _sent(b) == []


def test_backfill_skips_malformed_rows_without_stopping():
    """One bad row must not cost every other project its allocation."""
    b = _backfill_backend()
    _run(
        b.backfill_token_caps(
            [
                "not a dict",
                {"team_alias": None, "team_id": "t0", "metadata": {}},
                {"team_alias": "ok", "team_id": "t1", "metadata": {}},
            ]
        )
    )
    assert len(_sent(b)) == 1
    assert _sent(b)[0][1]["team_id"] == "t1"


def test_asking_which_pretend_projects_are_out_of_line_changes_nothing():
    """The question a watcher asks. Finding is separate from putting right."""
    teams = _litellm_with({PROJECT_FREE_TIER: [], PROJECT_SERVICE: [GROUP, "claude-opus-5"]})
    assert _run(teams.pretend_projects_out_of_line()) == {
        PROJECT_FREE_TIER: [],
        PROJECT_SERVICE: ["claude-opus-5", GROUP],
    }
    assert _updated(teams) == [] and _made(teams) == []
    assert _run(_litellm_with([PROJECT_FREE_TIER, PROJECT_SERVICE]).pretend_projects_out_of_line()) == {}
    # one with no team at all is out of line too
    assert _run(_litellm_with([PROJECT_FREE_TIER]).pretend_projects_out_of_line()) == {PROJECT_SERVICE: None}


def test_restricting_one_pretend_project_leaves_the_other_alone():
    teams = _litellm_with({PROJECT_FREE_TIER: [], PROJECT_SERVICE: []})
    _run(teams.restrict_pretend_project(PROJECT_FREE_TIER))
    assert _updated(teams) == [(f"id-{PROJECT_FREE_TIER}", [GROUP])]
    assert _run(teams.pretend_projects_out_of_line()) == {PROJECT_SERVICE: []}


def test_only_a_pretend_project_can_be_restricted_this_way():
    """A real project's team has no list on purpose."""
    teams = _litellm_with(["airflow"])
    with pytest.raises(ValueError):
        _run(teams.restrict_pretend_project("airflow"))
    assert _updated(teams) == []


def test_startup_stops_if_a_pretend_project_cannot_be_made_or_restricted():
    """Fail-fast: a service that cannot restrict free keys should not start."""

    def refuses_writes(existing):
        def litellm(call):
            if call.path == "team/list":
                return {"teams": existing}
            return (500, {"error": "no"})

        cfg = edict(budgets=edict(default_team_budget_usd=100, duration="30d", default_team_token_cap=0))
        return TeamManager(cfg, FakeTransport(litellm))

    with pytest.raises(BackendUnavailableError):
        _run(refuses_writes([]).ensure_pretend_projects())

    unrestricted = [{"team_alias": p, "team_id": f"id-{p}", "models": []} for p in PRETEND_PROJECTS]
    with pytest.raises(BackendUnavailableError):
        _run(refuses_writes(unrestricted).ensure_pretend_projects())

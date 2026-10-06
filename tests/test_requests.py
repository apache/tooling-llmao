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

"""Request lifecycle: asked, answered, exercised or expired.

The failures worth guarding are the quiet ones -- an approval that can be
used twice, one that never lapses, or a denial with no explanation attached.
"""

import datetime as dt

import pytest

from llmao.requests import (
    APPROVAL_TTL_DAYS,
    KIND_CAPACITY,
    KIND_KEY,
    METADATA_KEY,
    STATE_APPROVED,
    STATE_CONSUMED,
    STATE_DENIED,
    STATE_PENDING,
    Request,
    dump,
    load,
    prune,
    upsert,
)

UTC = dt.UTC


def _capacity(**kw):
    base = {
        "kind": KIND_CAPACITY,
        "requester": "akm",
        "project": "tooling",
        "reason": "weekly audit hits the cap on Thursday",
        "wanted": {"token_cap": 40_000_000},
    }
    base.update(kw)
    return Request.new(**base)


def _key(**kw):
    base = {
        "kind": KIND_KEY,
        "requester": "johndoe",
        "project": None,
        "reason": "nightly dependency scan",
        "wanted": {"purpose": "scrutineer-ci", "tier": "service"},
    }
    base.update(kw)
    return Request.new(**base)


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


def test_a_new_request_is_pending_with_an_id():
    r = _capacity()
    assert r.state == STATE_PENDING
    assert r.id and len(r.id) >= 8
    assert r.created_at


def test_ids_are_not_guessable():
    """They appear in form posts. A sequential id is one an admin can be
    tricked into approving."""
    ids = {_capacity().id for _ in range(50)}
    assert len(ids) == 50


def test_a_capacity_request_needs_a_project():
    with pytest.raises(ValueError):
        _capacity(project=None)


def test_a_key_request_does_not_need_one():
    assert _key().project is None


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        Request.new(kind="whatever", requester="akm", project="p", reason="", wanted={})


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def test_approving_as_asked_needs_no_reason():
    r = _capacity()
    r.decide(state=STATE_APPROVED, by="admin")
    assert r.state == STATE_APPROVED
    assert r.granted == r.wanted


def test_adjusting_requires_a_reason():
    """A request that comes back smaller with no explanation teaches people
    to pad the next one."""
    r = _capacity()
    with pytest.raises(ValueError):
        r.decide(state=STATE_APPROVED, by="admin", granted={"token_cap": 25_000_000})
    assert r.state == STATE_PENDING, "a refused decision must not half-apply"


def test_denying_requires_a_reason():
    r = _capacity()
    with pytest.raises(ValueError):
        r.decide(state=STATE_DENIED, by="admin")


def test_an_adjustment_keeps_what_was_asked_for():
    """The gap is the thing a requester wants to see."""
    r = _capacity()
    r.decide(state=STATE_APPROVED, by="admin", granted={"token_cap": 25_000_000}, reason="fleet is at 120% committed")
    assert r.wanted == {"token_cap": 40_000_000}
    assert r.granted == {"token_cap": 25_000_000}
    assert r.decision_reason


def test_a_request_cannot_be_decided_twice():
    r = _capacity()
    r.decide(state=STATE_APPROVED, by="admin")
    with pytest.raises(ValueError):
        r.decide(state=STATE_DENIED, by="other", reason="changed my mind")


# ---------------------------------------------------------------------------
# Exercising a key approval
# ---------------------------------------------------------------------------


def test_an_approved_key_request_is_actionable():
    r = _key()
    r.decide(state=STATE_APPROVED, by="admin")
    assert r.is_actionable()


def test_consuming_is_single_use():
    """The whole point: approval creates ONE key, not a standing right."""
    r = _key()
    r.decide(state=STATE_APPROVED, by="admin")
    r.consume()
    assert r.state == STATE_CONSUMED
    assert not r.is_actionable()
    with pytest.raises(ValueError):
        r.consume()


def test_a_pending_request_cannot_be_consumed():
    with pytest.raises(ValueError):
        _key().consume()


def test_a_denied_request_cannot_be_consumed():
    r = _key()
    r.decide(state=STATE_DENIED, by="admin", reason="no stated need")
    with pytest.raises(ValueError):
        r.consume()


def test_an_approval_expires():
    """An unused approval sitting for months is a latent credential nobody
    is thinking about."""
    r = _key()
    r.decide(state=STATE_APPROVED, by="admin")
    later = dt.datetime.now(UTC) + dt.timedelta(days=APPROVAL_TTL_DAYS + 1)
    assert r.is_expired(now=later)
    assert not r.is_actionable(now=later)


def test_an_approval_is_live_the_day_before_it_lapses():
    r = _key()
    r.decide(state=STATE_APPROVED, by="admin")
    nearly = dt.datetime.now(UTC) + dt.timedelta(days=APPROVAL_TTL_DAYS - 1)
    assert r.is_actionable(now=nearly)


def test_a_capacity_approval_does_not_expire():
    """It takes effect on approval; there is nothing to exercise later."""
    r = _capacity()
    r.decide(state=STATE_APPROVED, by="admin")
    far = dt.datetime.now(UTC) + dt.timedelta(days=365)
    assert not r.is_expired(now=far)


# ---------------------------------------------------------------------------
# Storage on team metadata
# ---------------------------------------------------------------------------


def test_round_trips_through_metadata():
    r = _capacity()
    r.decide(state=STATE_APPROVED, by="admin")
    meta = {"grantor": "Free Tier", METADATA_KEY: dump([r])}
    back = load(meta)
    assert len(back) == 1
    assert back[0].to_dict() == r.to_dict()


def test_load_ignores_anything_that_is_not_a_request():
    """Metadata is shared. One malformed entry must not cost the queue."""
    meta = {
        METADATA_KEY: [
            {"id": "x", "kind": KIND_KEY, "requester": "a"},
            {"no": "id"},
            "a string",
            None,
        ]
    }
    assert len(load(meta)) == 1


def test_load_on_metadata_without_requests():
    assert load({"grantor": "Free Tier"}) == []
    assert load(None) == []


def test_upsert_replaces_by_id():
    r = _capacity()
    stored = [r]
    r2 = Request.from_dict(r.to_dict())
    r2.decide(state=STATE_APPROVED, by="admin")
    out = upsert(stored, r2)
    assert len(out) == 1
    assert out[0].state == STATE_APPROVED


def test_prune_keeps_pending_and_drops_expired_approvals():
    pending = _capacity()
    stale = _key()
    stale.decide(state=STATE_APPROVED, by="admin")
    later = dt.datetime.now(UTC) + dt.timedelta(days=APPROVAL_TTL_DAYS + 1)
    out = prune([pending, stale], now=later)
    assert [r.id for r in out] == [pending.id]


def test_prune_caps_decided_history():
    """Metadata is a blob on a row and every read of the team carries it."""
    reqs = []
    for i in range(80):
        r = _capacity()
        r.decide(state=STATE_DENIED, by="admin", reason=f"no {i}")
        reqs.append(r)
    out = prune(reqs, keep_decided=50)
    assert len(out) == 50


def test_prune_never_drops_a_pending_request():
    reqs = [_capacity() for _ in range(80)]
    assert len(prune(reqs, keep_decided=5)) == 80


def test_setting_an_allowance_on_a_key_is_not_an_adjustment():
    """Adding a field the requester never named is completing the request.

    A projectless service key has nothing to draw on, so an admin sets its
    allowance at approval -- the requester has no business naming it. Making
    that need a justification would train everyone to type "ok" in the box,
    which costs the reason field its meaning on decisions that do need one.
    """
    r = _key()
    r.decide(state=STATE_APPROVED, by="admin", granted={"token_cap": 2_000_000})
    assert r.state == STATE_APPROVED
    assert r.granted["token_cap"] == 2_000_000
    assert r.granted["purpose"] == "scrutineer-ci"


def test_changing_a_value_the_requester_named_is_an_adjustment():
    r = _key()
    with pytest.raises(ValueError):
        r.decide(state=STATE_APPROVED, by="admin", granted={"tier": "free_tier"})


def test_granted_carries_everything_asked_for_plus_what_was_added():
    r = _key()
    r.decide(state=STATE_APPROVED, by="admin", granted={"token_cap": 500_000})
    assert set(r.granted) == {"purpose", "tier", "token_cap"}


def test_an_approved_as_asked_request_has_granted_equal_to_wanted():
    """So the UI can tell "approved as asked" from "approved, but".

    A row reading "30M, asked 30M" is noise on every unmodified approval;
    one reading "30M, asked 40M" is the thing the requester needs to see.
    """
    r = _capacity()
    r.decide(state=STATE_APPROVED, by="admin")
    assert r.granted == r.wanted


def test_an_adjusted_request_differs_from_what_was_asked():
    r = _capacity()
    r.decide(
        state=STATE_APPROVED,
        by="admin",
        granted={"token_cap": 30_000_000},
        reason="fleet is heavily committed",
    )
    assert r.granted != r.wanted
    assert r.granted["token_cap"] == 30_000_000
    assert r.wanted["token_cap"] == 40_000_000

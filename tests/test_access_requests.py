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

import asyncio
import datetime as dt
import json

import pytest
from easydict import EasyDict as edict  # noqa: N813
from fake_transport import FakeTransport

from llmao import access_requests
from llmao.access_requests import (
    APPROVAL_TTL_DAYS,
    KIND_CAPACITY,
    KIND_KEY,
    STATE_APPROVED,
    STATE_CONSUMED,
    STATE_DENIED,
    STATE_PENDING,
    AccessRequest,
    dumps,
    key_for,
    loads,
    prune,
)
from llmao.proxy.client import LiteLLMBackend

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
    return AccessRequest.new(**base)


def _key(**kw):
    base = {
        "kind": KIND_KEY,
        "requester": "johndoe",
        "project": None,
        "reason": "nightly dependency scan",
        "wanted": {"purpose": "scrutineer-ci", "tier": "service"},
    }
    base.update(kw)
    return AccessRequest.new(**base)


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
        AccessRequest.new(kind="whatever", requester="akm", project="p", reason="", wanted={})


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
# Storage in LiteLLM's memory store
# ---------------------------------------------------------------------------


def test_round_trips_through_a_stored_value():
    r = _capacity()
    r.decide(state=STATE_APPROVED, by="admin")
    back = loads(dumps(r))
    assert back.to_dict() == r.to_dict()


def test_loads_ignores_anything_that_is_not_a_request():
    """The store is shared. One malformed row must not cost the queue."""
    assert loads(json.dumps({"id": "x", "kind": KIND_KEY, "requester": "a"})) is not None
    for junk in (json.dumps({"no": "id"}), json.dumps("a string"), "not json at all", None):
        assert loads(junk) is None


def test_every_request_key_shares_the_prefix_listing_asks_for():
    assert key_for("abc") == "llmao:request:abc"
    assert key_for("abc").startswith(access_requests.KEY_PREFIX)


def test_prune_keeps_pending_and_drops_expired_approvals():
    pending = _capacity()
    stale = _key()
    stale.decide(state=STATE_APPROVED, by="admin")
    later = dt.datetime.now(UTC) + dt.timedelta(days=APPROVAL_TTL_DAYS + 1)
    out = prune([pending, stale], now=later)
    assert [r.id for r in out] == [pending.id]


def test_prune_caps_decided_history():
    """Nothing else trims the store, and every listing carries all of it."""
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


# ---------------------------------------------------------------------------
# The backend, against a memory store that behaves like LiteLLM's
#
# Requests and tier overrides used to live in team metadata. The point of
# moving them is that reading them no longer touches a team at all, so these
# fail on any call that is not to /v1/memory.
# ---------------------------------------------------------------------------


def _backend(*, down=False):
    """A real backend over a memory store that behaves like LiteLLM's.

    `b.store` is what the store holds, key to value.
    """
    store = {}

    def answer(call):
        assert call.path == "v1/memory" or call.path.startswith("v1/memory/"), f"unexpected call: {call}"
        key = call.path.removeprefix("v1/memory").lstrip("/")
        if call.method == "GET" and not key:
            hits = [{"key": k, "value": v} for k, v in store.items() if k.startswith(call.params.key_prefix)]
            return {"memories": hits, "total": len(hits)}
        if call.method == "GET":
            return {"key": key, "value": store[key]} if key in store else (404, {})
        if call.method == "PUT":
            store[key] = call.json.value
            return {"key": key, "value": store[key]}
        if call.method == "DELETE":
            return {"deleted": True} if store.pop(key, None) is not None else (404, {})
        raise AssertionError(f"unexpected call: {call}")

    cfg = edict(
        entitlements=edict(
            default=edict(rpm_limit=60, tpm_limit=60000, max_parallel_requests=4),
            free_tier=edict(token_cap=1_000_000, token_window_days=7),
        )
    )
    b = LiteLLMBackend(cfg, None, transport=FakeTransport(answer, down=down))
    b.store = store
    return b


def _run(coro):
    return asyncio.run(coro)


def test_a_stored_request_is_listed_and_found():
    b = _backend()
    r = _key()
    _run(b.add_request(r))
    assert list(b.store) == [f"llmao:request:{r.id}"]
    assert [x.id for x in _run(b.list_requests())] == [r.id]
    assert _run(b.find_request(r.id)).to_dict() == r.to_dict()
    assert _run(b.find_request("no-such-id")) is None


def test_listing_for_a_project_leaves_the_others_out():
    b = _backend()
    mine, theirs, nobodys = _capacity(project="tooling"), _capacity(project="mahout"), _key()
    for r in (mine, theirs, nobodys):
        _run(b.add_request(r))
    assert [x.id for x in _run(b.list_requests("tooling"))] == [mine.id]
    assert len(_run(b.list_requests())) == 3


def test_a_decision_rewrites_one_row_and_no_other():
    """In team metadata, answering one request rewrote all of them."""
    b = _backend()
    first, second = _capacity(), _capacity()
    _run(b.add_request(first))
    _run(b.add_request(second))
    untouched = b.store[key_for(second.id)]

    found = _run(b.find_request(first.id))
    found.decide(state=STATE_APPROVED, by="admin")
    _run(b.save_request(found))

    assert _run(b.find_request(first.id)).state == STATE_APPROVED
    assert b.store[key_for(second.id)] == untouched
    assert len(b.store) == 2


def test_a_lapsed_approval_is_deleted_on_the_next_write():
    b = _backend()
    stale = _key()
    stale.decide(state=STATE_APPROVED, by="admin")
    long_ago = dt.datetime.now(UTC) - dt.timedelta(days=APPROVAL_TTL_DAYS + 1)
    stale.decided_at = long_ago.isoformat(timespec="seconds")
    b.store[key_for(stale.id)] = dumps(stale)

    fresh = _capacity()
    _run(b.add_request(fresh))
    assert list(b.store) == [key_for(fresh.id)]


def test_tier_limits_are_config_until_an_admin_changes_them():
    b = _backend()
    assert _run(b.tier_entitlement("free_tier"))["token_cap"] == 1_000_000
    got = _run(b.set_tier_entitlement("free_tier", token_cap=250_000))
    assert got["token_cap"] == 250_000
    assert got["token_window_days"] == 7, "what was not changed still comes from config"
    assert list(b.store) == ["llmao:tiers"]


def test_changing_one_tier_leaves_another_alone():
    """Every tier shares the one row."""
    b = _backend()
    _run(b.set_tier_entitlement("free_tier", token_cap=250_000))
    _run(b.set_tier_entitlement("service", rpm_limit=10))
    _run(b.set_tier_entitlement("free_tier", token_window_days=3))
    assert json.loads(b.store["llmao:tiers"]) == {
        "free_tier": {"token_cap": 250_000, "token_window_days": 3},
        "service": {"rpm_limit": 10},
    }


def test_tier_limits_fall_back_to_config_when_litellm_is_down():
    """A page must still have limits to show during a proxy restart."""
    assert _run(_backend(down=True).tier_entitlement("free_tier"))["token_cap"] == 1_000_000

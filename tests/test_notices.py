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
"""Skew and log: what llmao tells people, kept in LiteLLM's memory store.

Skew is work that needs doing, and a row stays until the work is done. A log
entry is something to be seen, and goes when it has been.
"""

import asyncio
import json

from llmao.proxy.client import LogManager, MemoryStore, SkewManager
from tests.fake_transport import fake_memory


def _run(coro):
    return asyncio.run(coro)


def _skew():
    transport, store = fake_memory()
    return SkewManager(MemoryStore(transport)), store, transport


def _log():
    transport, store = fake_memory()
    return LogManager(MemoryStore(transport)), store


def _writes(transport):
    return [(c.method, c.path) for c in transport.calls if c.method != "GET"]


# ---------------------------------------------------------------------------
# Skew: the check owns the rows
# ---------------------------------------------------------------------------


def test_what_a_check_finds_becomes_a_row_each():
    skew, store, _ = _skew()
    _run(skew.reconcile("key", {"tok1": {"what": "limits differ from the service tier", "owner": "alice"}}))
    assert list(store) == ["llmao:skew:key:tok1"]
    (found,) = _run(skew.open())
    assert (found.kind, found.subject, found.owner) == ("key", "tok1", "alice")
    assert found.what == "limits differ from the service tier"
    assert found.key == "llmao:skew:key:tok1"


def test_a_row_goes_when_the_check_no_longer_finds_it():
    """Fixed is the only way a skew row ends."""
    skew, store, _ = _skew()
    _run(skew.reconcile("key", {"tok1": {"what": "a"}, "tok2": {"what": "b"}}))
    _run(skew.reconcile("key", {"tok2": {"what": "b"}}))
    assert list(store) == ["llmao:skew:key:tok2"]


def test_a_check_leaves_other_kinds_alone():
    skew, store, _ = _skew()
    _run(skew.reconcile("deployment", {"gemma": {"what": "not marked self-hosted"}}))
    _run(skew.reconcile("key", {}))
    assert list(store) == ["llmao:skew:deployment:gemma"]
    assert [n.subject for n in _run(skew.open("deployment"))] == ["gemma"]
    assert _run(skew.open("key")) == []


def test_a_problem_that_sits_is_not_rewritten_on_every_pass():
    skew, _, transport = _skew()
    _run(skew.reconcile("key", {"tok1": {"what": "a"}}))
    before = len(_writes(transport))
    _run(skew.reconcile("key", {"tok1": {"what": "a"}}))
    assert len(_writes(transport)) == before
    _run(skew.reconcile("key", {"tok1": {"what": "something else now"}}))
    assert len(_writes(transport)) == before + 1


def test_putting_one_thing_right_clears_it_at_once():
    skew, store, _ = _skew()
    _run(skew.reconcile("key", {"tok1": {"what": "a"}}))
    _run(skew.clear("key", "tok1"))
    assert store == {}


# ---------------------------------------------------------------------------
# Log: for the admins, or for one person
# ---------------------------------------------------------------------------


def test_an_entry_is_for_the_admins_unless_it_names_a_person():
    log, store = _log()
    _run(log.note("the skew check failed"))
    _run(log.note("a site admin revoked your key ci", user="alice"))
    assert [n.what for n in _run(log.unseen())] == ["the skew check failed"]
    assert [n.what for n in _run(log.unseen("alice"))] == ["a site admin revoked your key ci"]
    assert _run(log.unseen("bob")) == []
    assert sorted(k.rsplit(":", 1)[0] for k in store) == ["llmao:log:admin", "llmao:log:user:alice"]


def test_acknowledging_removes_the_entry():
    log, store = _log()
    _run(log.note("the skew check failed"))
    (entry,) = _run(log.unseen())
    _run(log.ack(entry.key))
    assert store == {}


def test_the_same_thing_happening_again_is_one_entry_with_a_count():
    """Found on every pass of a check, it is one line to read."""
    log, store = _log()
    for _ in range(3):
        _run(log.note("free tier allowed any model; restricted", subject="project:free-tier"))
    assert list(store) == ["llmao:log:admin:project:free-tier"]
    assert json.loads(store["llmao:log:admin:project:free-tier"])["count"] == 3


def test_separate_events_are_separate_entries():
    log, store = _log()
    _run(log.note("one"))
    _run(log.note("two"))
    assert len(store) == 2


def test_an_entry_can_be_checked_against_who_is_acknowledging_it():
    """One person must not be able to clear another's, or the admins'."""
    log, _ = _log()
    _run(log.note("yours", user="alice"))
    (entry,) = _run(log.unseen("alice"))
    assert log.is_for(entry.key, "alice")
    assert not log.is_for(entry.key, "bob")
    assert not log.is_for(entry.key)

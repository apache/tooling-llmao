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

"""bin/sync-nodeping.py: the plan from servers and checks, and what each action sends."""

import importlib.util
import pathlib

from easydict import EasyDict as edict  # noqa: N813

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "bin" / "sync-nodeping.py"


def _load():
    spec = importlib.util.spec_from_file_location("sync_nodeping", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync = _load()


def _server(model, host, port, url):
    return edict(model=model, host=host, listen_port=port, provider=url and "Vast", health_url=url)


def _check(check_id, label, target, created=1):
    return {"_id": check_id, "label": label, "created": created, "parameters": {"target": target, "sens": 5}}


def _by_id(*checks):
    return {c["_id"]: c for c in checks}


def test_plan_covers_every_case_in_one_fleet():
    servers = [
        _server("qwen3-8b", "58458812", 8001, "http://203.0.113.5:18490/health"),  # unchanged
        _server("gemma4-26b", "58458812", 8002, "http://203.0.113.5:18491/health"),  # target moved
        _server("qwen3.8-27b", "3dc7441nqbv0io", 8001, "http://203.0.113.9:1234/health"),  # no check yet
        _server("qwen3-8b", "59081243", 8001, None),  # lost its address; check stands
        _server("qwen3-8b", "61000184", 8001, None),  # never placed; nothing to make
    ]
    checks = _by_id(
        _check("A-1", "rai qwen3-8b on 58458812:8001", "http://203.0.113.5:18490/health"),
        _check("A-2", "rai gemma4-26b on 58458812:8002", "http://198.51.100.2:18491/health"),
        _check("A-3", "rai qwen3-8b on 59081243:8001", "http://198.51.100.7:1/health"),
        _check("A-4", "uvicorn-test", "http://198.51.100.8:18490/health"),
        _check("A-5", "rai qwen3-8b on 58458812:8001", "http://203.0.113.5:18490/health", created=2),  # a duplicate
    )
    actions = sync.plan("rai", servers, checks)
    by_label = {}
    for act in actions:
        by_label.setdefault(act.label, []).append(act)
    assert [a.action for a in by_label["rai qwen3-8b on 58458812:8001"]] == [sync.DELETE, sync.UNCHANGED]
    assert by_label["rai qwen3-8b on 58458812:8001"][0].check["_id"] == "A-5"
    assert by_label["rai gemma4-26b on 58458812:8002"][0].action == sync.UPDATE
    assert by_label["rai gemma4-26b on 58458812:8002"][0].target == "http://203.0.113.5:18491/health"
    assert by_label["rai qwen3.8-27b on 3dc7441nqbv0io:8001"][0].action == sync.CREATE
    assert by_label["rai qwen3-8b on 59081243:8001"][0].action == sync.KEEP
    assert by_label["rai qwen3-8b on 61000184:8001"][0].action == sync.WAIT
    assert by_label["uvicorn-test"][0].action == sync.DELETE


class _Recorder:
    def __init__(self):
        self.calls = []

    def create_check(self, body):
        self.calls.append(("create", body))
        return {}

    def update_check(self, check_id, body):
        self.calls.append(("update", check_id, body))
        return {}

    def delete_check(self, check_id):
        self.calls.append(("delete", check_id))
        return {}


def test_apply_sends_the_sample_shape_on_create_and_only_the_target_on_update(capsys):
    actions = [
        edict(
            action=sync.CREATE,
            label="rai m on h:8001",
            check=None,
            target="http://203.0.113.5:1/health",
            reason="no check",
        ),
        edict(
            action=sync.UPDATE,
            label="rai m on h:8002",
            check={"_id": "A-2"},
            target="http://203.0.113.5:2/health",
            reason="x",
        ),
        edict(action=sync.DELETE, label="old", check={"_id": "A-3"}, target=None, reason="gone"),
        edict(action=sync.KEEP, label="rai m on h:8003", check={"_id": "A-4"}, target=None, reason="no address"),
        edict(
            action=sync.UNCHANGED, label="rai m on h:8004", check={"_id": "A-5"}, target="http://x/health", reason=""
        ),
    ]
    np = _Recorder()
    counts = sync.apply(np, "rai", actions, ["slack1"], dry_run=False, verbose=False)
    assert counts == {sync.CREATE: 1, sync.UPDATE: 1, sync.DELETE: 1, sync.KEEP: 1, sync.UNCHANGED: 1}
    assert np.calls[0] == (
        "create",
        {
            "type": "HTTP",
            "interval": 5,
            "threshold": 5,
            "sens": 5,
            "enabled": "true",
            "label": "rai m on h:8001",
            "target": "http://203.0.113.5:1/health",
            "tags": ["rai"],
            "notifications": [{"slack1": {"delay": 0, "schedule": "All"}}],
        },
    )
    assert np.calls[1] == ("update", "A-2", {"target": "http://203.0.113.5:2/health"})
    assert np.calls[2] == ("delete", "A-3")
    assert len(np.calls) == 3
    out = capsys.readouterr().out
    assert "unchanged" not in out
    assert (
        sync.summary(counts, dry_run=False)
        == "nodeping: 1 created, 1 updated, 1 deleted, 1 kept without an address, 1 unchanged"
    )


def test_dry_run_sends_nothing_and_says_would():
    actions = [edict(action=sync.DELETE, label="old", check={"_id": "A-3"}, target=None, reason="gone")]
    np = _Recorder()
    counts = sync.apply(np, "rai", actions, ["slack1"], dry_run=True, verbose=False)
    assert np.calls == []
    assert sync.summary(counts, dry_run=True) == "nodeping (dry run): 1 deleted"


def test_slack_addresses_are_every_slack_address_in_the_account():
    contacts = {
        "C-1": {"name": "rai-alerts", "addresses": {"ixizv3lu": {"type": "slack", "address": "https://hooks/x"}}},
        "C-2": {"name": "Infra", "addresses": {"JV2DEZGZ": {"type": "email", "address": "root@example.org"}}},
    }
    assert sync.slack_addresses(contacts) == ["ixizv3lu"]
    assert sync.slack_addresses({"C-2": contacts["C-2"]}) == []

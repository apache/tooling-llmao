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

"""In-memory host-scoped admin labels (Approved, Reboot requested).

These are annotations on a box, not lifecycle states: the contract under test
is the get/set API, the loss-on-restart guarantee (a fresh Fleet has none),
and -- the core safety property -- that a label never changes the computed
lifecycle.
"""

import pytest
from easydict import EasyDict as edict  # noqa: N813

from llmao.fleet import Fleet, FleetDeployment, VllmServer, snapshot_for_status
from llmao.model_status import (
    ADMIN_LABELS,
    APPROVED,
    AWAITING,
    REBOOT_REQUESTED,
    deployment_status,
)
from tests.test_fleet import FLEET_KNOBS

HOST = "10.0.0.1"


def _server(**kwargs):
    defaults = dict(
        model_name="gemma4-26b",
        name="gemma4-26b",
        host=HOST,
        listen_port=8001,
        hf_model="google/gemma",
        api_key="sk-x",
        args=[],
        public_port=8001,
    )
    defaults.update(kwargs)
    return VllmServer(**defaults)


def _fleet(**server_kwargs):
    return Fleet(cfg=edict({"fleet": dict(FLEET_KNOBS)}), servers=[_server(**server_kwargs)])


def test_fresh_fleet_has_no_labels():
    # The loss-on-restart contract: a new process starts with no admin labels.
    fleet = _fleet()
    assert fleet.admin_label(HOST) == (None, None)
    assert fleet.admin_labels == {}
    assert fleet.admin_label_at == {}


def test_set_and_read_round_trips():
    fleet = _fleet()
    fleet.set_admin_label(HOST, APPROVED, now=100.0)
    assert fleet.admin_label(HOST) == (APPROVED, 100.0)


def test_keys_on_the_normalized_host():
    # Same host presented as an IPv4-mapped IPv6 address is the same label.
    fleet = _fleet()
    fleet.set_admin_label("::ffff:10.0.0.1", APPROVED, now=100.0)
    assert fleet.admin_label(HOST) == (APPROVED, 100.0)


def test_clear_drops_both_stores():
    fleet = _fleet()
    fleet.set_admin_label(HOST, APPROVED, now=100.0)
    fleet.set_admin_label(HOST, None, now=200.0)
    assert fleet.admin_label(HOST) == (None, None)
    assert fleet.admin_labels == {}
    assert fleet.admin_label_at == {}


def test_change_updates_timestamp():
    fleet = _fleet()
    fleet.set_admin_label(HOST, APPROVED, now=100.0)
    fleet.set_admin_label(HOST, REBOOT_REQUESTED, now=500.0)
    assert fleet.admin_label(HOST) == (REBOOT_REQUESTED, 500.0)


def test_unknown_label_raises():
    fleet = _fleet()
    with pytest.raises(ValueError, match="Unknown admin label"):
        fleet.set_admin_label(HOST, "retired", now=100.0)
    # Nothing was recorded.
    assert fleet.admin_label(HOST) == (None, None)
    assert "retired" not in ADMIN_LABELS


def test_label_is_orthogonal_to_lifecycle():
    # A label is an annotation on top of the computed state, never an input.
    fleet = _fleet()
    srv = fleet.servers[0]

    def status():
        # snapshot_for_status wants the nested block; deployment_status reads cfg.health_* directly.
        return deployment_status(
            snapshot_for_status(FleetDeployment.from_vllm(srv), edict({"fleet": dict(FLEET_KNOBS)}), now=1000.0),
            edict(FLEET_KNOBS),
            now=1000.0,
        )

    # No config served, no probe yet: Awaiting contact.
    before = status()
    assert before.lifecycle == AWAITING

    fleet.set_admin_label(HOST, APPROVED, now=999.0)
    assert status().lifecycle == AWAITING  # same, untouched

    # Drive a probe, then relabel mid-loading: still Loading.
    srv.config_served_at = 995.0
    srv.record_probe(False, now=1000.0, grace_s=1800, fail_threshold=3, err="refused")
    mid = status()
    assert mid.lifecycle != AWAITING
    fleet.set_admin_label(HOST, REBOOT_REQUESTED, now=1000.0)
    after = status()
    assert (after.lifecycle, after.counted, after.skew) == (mid.lifecycle, mid.counted, mid.skew)

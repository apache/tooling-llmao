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
"""Startup says so when LiteLLM's price list is never refreshed."""

from __future__ import annotations

import asyncio
import logging

from easydict import EasyDict as edict  # noqa: N813

from llmao.proxy.client import LiteLLMBackend
from tests.fake_transport import FakeTransport

STATUS = "schedule/model_cost_map_reload/status"


def _check(answer, caplog, *, down=False):
    fake = FakeTransport(lambda call: answer if call.path == STATUS else (404, {}), down=down)
    backend = LiteLLMBackend(edict(), None, transport=fake)
    with caplog.at_level(logging.INFO, logger="llmao.proxy.client"):
        asyncio.run(backend.check_cost_map_schedule())
    return [(r.levelname, r.getMessage()) for r in caplog.records]


def test_no_schedule_is_an_error(caplog):
    logged = _check({"scheduled": False, "interval_hours": None}, caplog)
    assert [lvl for lvl, _ in logged] == ["ERROR"]
    assert "POST /schedule/model_cost_map_reload?hours=24" in logged[0][1]


def test_a_schedule_is_noted(caplog):
    logged = _check({"scheduled": True, "interval_hours": 24}, caplog)
    assert logged == [("INFO", "LiteLLM reloads its price list every 24 hours")]


def test_an_unreadable_schedule_is_a_warning_not_a_failed_start(caplog):
    assert [lvl for lvl, _ in _check((404, {}), caplog)] == ["WARNING"]
    caplog.clear()
    assert [lvl for lvl, _ in _check({}, caplog, down=True)] == ["WARNING"]

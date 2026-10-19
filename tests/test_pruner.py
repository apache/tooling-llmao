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
"""The daily pruner (llmao/pruner.py)."""

from __future__ import annotations

import asyncio
import logging

from llmao.pruner import Pruner


def test_every_job_runs_and_a_failure_stops_none_of_the_others(caplog):
    ran = []

    def job(name, gone):
        async def prune():
            ran.append(name)
            if gone is None:
                raise RuntimeError("LiteLLM away")
            return gone

        return (name, prune)

    pruner = Pruner([job("requests", 3), job("broken", None), job("actions", 0)])
    with caplog.at_level(logging.INFO, logger="llmao.pruner"):
        done = asyncio.run(pruner.prune_all())
    assert ran == ["requests", "broken", "actions"]
    assert done == {"requests": 3, "broken": None, "actions": 0}
    assert "prune: removed 3 requests" in caplog.text
    assert "prune: broken failed" in caplog.text
    # Nothing removed is nothing to say.
    assert "removed 0" not in caplog.text


def test_an_empty_list_prunes_nothing():
    assert asyncio.run(Pruner([]).prune_all()) == {}

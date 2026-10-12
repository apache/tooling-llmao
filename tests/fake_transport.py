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
"""A stand-in for LiteLLMTransport, so tests need no proxy.

Hand it a function that answers a call, and hand IT to the code under test:

    fake = FakeTransport(lambda call: {"teams": []})
    backend = LiteLLMBackend(cfg, fleet, transport=fake)
    ...
    assert [c.path for c in fake.calls] == ["team/list"]

The function gets the call (method, path, params, json) and returns the
JSON body to answer with, or `(status, body)` when the status matters. With
no function every call is answered `{}`.
"""

from __future__ import annotations

from easydict import EasyDict as edict  # noqa: N813

from llmao.litellm_client import BackendUnavailableError


class FakeTransport:
    def __init__(self, answer=None, *, down: bool = False):
        self._answer = answer
        # True behaves like a proxy that cannot be reached at all.
        self.down = down
        # Every call made, in order.
        self.calls: list[edict] = []

    async def request(self, method: str, path: str, *, params=None, json=None, **kwargs):
        call = edict(method=method, path=path, params=params or {}, json=json)
        self.calls.append(call)
        if self.down:
            raise BackendUnavailableError("could not reach LiteLLM")
        answer = self._answer(call) if self._answer else {}
        status, body = answer if isinstance(answer, tuple) else (200, answer)
        return edict(status_code=status, content=b"x", text=str(body), json=lambda: body)

    def raise_http(self, resp) -> None:
        if resp.status_code >= 400:
            raise BackendUnavailableError(f"LiteLLM admin error {resp.status_code}: {resp.text}")

    async def aclose(self) -> None:
        return None

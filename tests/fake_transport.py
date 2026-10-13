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

from llmao.proxy.client import BackendUnavailableError


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


def memory_answers():
    """How LiteLLM's /v1/memory answers, and what it holds.

    Returns (answer, store), for a test whose stand-in LiteLLM has to answer
    other calls as well. `store` is the rows, key to value. The answer
    function fails on any call that is not to /v1/memory.
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

    return answer, store


def fake_memory(*, down: bool = False):
    """A FakeTransport that behaves like LiteLLM's /v1/memory, and what it holds.

    Returns (transport, store). `store` is the rows, key to value, for a
    test to seed or to look at.
    """
    answer, store = memory_answers()
    return FakeTransport(answer, down=down), store

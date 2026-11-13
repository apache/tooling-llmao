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
"""Gives an httpx double `stream`, written in terms of its own `get`.

vllm_client reads /v1/models and /metrics with ``client.stream`` so it can
stop at a byte cap rather than hold whatever a rental decides to send.
The doubles in these tests answer ``get``, which is the shape that reads
well in a test, so this bridges the two rather than making every double
implement streaming:

    class _Client(StreamsViaGet):
        async def get(self, url, headers=None):
            ...
"""

from __future__ import annotations

import json


class _Streamed:
    """A streamed response, over the one a double already built."""

    def __init__(self, resp):
        self._resp = resp
        self.status_code = getattr(resp, "status_code", 200)

    async def aiter_bytes(self):
        body = getattr(self._resp, "content", None)
        if not body:
            text = getattr(self._resp, "text", None)
            if text and text != "OK":
                body = text.encode()
            else:
                payload = self._resp.json() if hasattr(self._resp, "json") else None
                body = json.dumps(payload).encode() if payload is not None else b""
        yield body


class StreamsViaGet:
    def stream(self, method: str, url: str, headers=None):
        double = self

        class _Ctx:
            async def __aenter__(self):
                try:
                    resp = await double.get(url, headers=headers)
                except TypeError:
                    resp = await double.get(url)
                return _Streamed(resp)

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

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

"""Admin index: no Models row, and the LiteLLM version against the pin."""

import asyncio
import io
import pathlib

import ezt
import httpx
from easydict import EasyDict as edict  # noqa: N813

from llmao.litellm_client import LiteLLMBackend, pinned_litellm_version

THIS_DIR = pathlib.Path(__file__).resolve().parent.parent


def _render(*, running, expected):
    data = edict(
        title="Admin",
        is_site_admin=ezt.boolean(True),
        restarted_at="10/08 14:30 (UTC)",
        reveal_supply=False,
        uid="u@example.apache.org",
        name="N",
        can_create_automation=ezt.boolean(False),
        projects=[],
        projects_label=None,
        admin_projects=[],
        flashes=[],
        repo="https://github.com/apache/tooling-llmao",
        commit="deadbeef",
        pending_count=None,
        litellm_running=running,
        litellm_expected=expected,
        litellm_matches=ezt.boolean(bool(running) and running == expected),
    )
    buf = io.StringIO()
    template = ezt.Template(
        str(THIS_DIR / "templates" / "admin.ezt"), compress_whitespace=0, base_format=ezt.FORMAT_HTML
    )
    template.generate(buf, data)
    return buf.getvalue()


def test_admin_drops_models_and_shows_a_version_match():
    pin = pinned_litellm_version()
    out = _render(running=pin, expected=pin)
    assert "Routes, hosts and serving state" not in out
    assert f"{pin}, matches the pin." in out


def test_admin_shows_a_version_mismatch_and_unavailable():
    out = _render(running="1.0.0", expected="1.102.2")
    assert "Running 1.0.0. The pin is 1.102.2." in out
    missing = _render(running=None, expected="1.102.2")
    assert "Unavailable." in missing


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = ""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://litellm.test/health/readiness/details")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("no", request=request, response=response)


class _Client:
    def __init__(self, resp=None, exc=None):
        self.resp = resp
        self.exc = exc
        self.path = None

    async def request(self, method, path, **kwargs):
        self.path = path
        if self.exc:
            raise self.exc
        return self.resp

    async def aclose(self):
        return None


def _backend(client):
    cfg = edict(litellm=edict(base_url="http://litellm.test", master_key="k", request_timeout_s=1))
    backend = LiteLLMBackend(cfg, fleet=None)
    backend._client = client
    return backend


def test_proxy_version_reads_the_details_endpoint():
    client = _Client(_Resp(200, {"litellm_version": "1.102.2"}))
    backend = _backend(client)

    async def run():
        assert await backend.proxy_version() == "1.102.2"
        await client.aclose()

    asyncio.run(run())
    assert client.path == "health/readiness/details"


def test_proxy_version_is_unavailable_when_litellm_cannot_be_reached():
    backend = _backend(_Client(exc=httpx.ConnectError("refused")))

    async def run():
        assert await backend.proxy_version() is None

    asyncio.run(run())

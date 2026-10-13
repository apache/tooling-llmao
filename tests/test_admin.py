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

from llmao.proxy.client import LiteLLMBackend, LiteLLMTransport, pinned_litellm_version

THIS_DIR = pathlib.Path(__file__).resolve().parent.parent


def _render(*, running, expected, skew=(), flashes=(), notices=(), work=None):
    data = edict(
        title="Admin",
        is_site_admin=ezt.boolean(True),
        nav="admin",
        login_back="/admin",
        restarted_at="10/08 14:30 (UTC)",
        reveal_supply=False,
        uid="u@example.apache.org",
        name="N",
        can_create_automation=ezt.boolean(False),
        projects=[],
        projects_label=None,
        admin_projects=[],
        flashes=list(flashes),
        notices=list(notices),
        work=work,
        repo="https://github.com/apache/tooling-llmao",
        commit="deadbeef",
        pending_count=None,
        skew=list(skew),
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
    return LiteLLMBackend(cfg, fleet=None, transport=LiteLLMTransport(cfg, client=client))


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


def test_admin_shows_work_needed():
    """Skew stays until it is put right, so it has no button to dismiss it."""
    pin = pinned_litellm_version()
    skew = [edict(what="limits differ from the service tier", about="key t1", since="2026-10-12 09:00 UTC")]
    out = _render(running=pin, expected=pin, skew=skew)
    assert "Work needed" in out
    assert "limits differ from the service tier" in out
    assert "key t1" in out
    assert "since 2026-10-12 09:00 UTC" in out
    assert "/do-acknowledge" not in out
    assert "Work needed" not in _render(running=pin, expected=pin)


def test_something_to_see_is_shown_on_the_page_and_can_be_acknowledged():
    """The same partial on every page, so there is one place a person is told things."""
    pin = pinned_litellm_version()
    told = edict(
        what="free tier allowed any model; restricted",
        key="llmao:log:admin:project:free-tier",
        times="3 times",
        when="2026-10-13 01:02 UTC",
    )
    out = _render(running=pin, expected=pin, notices=[told])
    assert "Notices" in out
    assert "free tier allowed any model; restricted" in out
    assert "3 times" in out
    assert 'name="key" value="llmao:log:admin:project:free-tier"' in out
    assert out.count("/do-acknowledge") == 1
    assert "Notices" not in _render(running=pin, expected=pin)


def test_what_stays_is_kept_apart_from_what_goes():
    """A flash is gone on the next page; an entry stays until acknowledged.

    Shown as the same kind of box, one would vanish and the other would not,
    and nobody could tell why.
    """
    pin = pinned_litellm_version()
    flash = edict(category="success", message="Tier updated.")
    told = edict(what="a site admin revoked your key ci", key="llmao:log:user:alice:x", times="", when="")
    out = _render(running=pin, expected=pin, flashes=[flash], notices=[told])
    card = out[out.index("Notices") :]
    card = card[: card.index("</div>\n</div>")]
    # the entry is in its own card, with the only Acknowledge button, and says it stays
    assert "a site admin revoked your key ci" in card
    assert "Tier updated." not in card
    assert "stays until you acknowledge it" in out
    # the flash is an alert that can be closed, and has no Acknowledge
    alert = out[out.index("alert-success") : out.index("Notices")]
    assert "Tier updated." in alert
    assert "btn-close" in alert
    assert "/do-acknowledge" not in alert


def test_work_needed_is_one_line_saying_where_to_go():
    """Somebody reading Help is told work is waiting, and is not made to look at it."""
    pin = pinned_litellm_version()
    work = edict(what="3 things need doing.", href="/keys", label="My Keys")
    out = _render(running=pin, expected=pin, work=work)
    assert "3 things need doing." in out
    assert 'href="/keys">Go to My Keys</a>' in out
    assert "3 things need doing." not in _render(running=pin, expected=pin)

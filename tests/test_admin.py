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


def _render(*, running, expected, skew=(), flashes=(), notices=(), work=None, requests=(), approvals=(), recent=()):
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
        pending_count=len(requests) or None,
        requests=list(requests),
        approvals=list(approvals),
        approval_count=len(approvals) or None,
        recent=list(recent),
        recent_days=14,
        recent_mine=ezt.boolean(False),
        skew=list(skew),
        skew_count=len(skew) or None,
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
    assert "Running 1.0.0; the pin is 1.102.2." in out
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
    skew = [
        edict(
            what="limits differ from the service tier",
            subject="t1",
            action="",
            action_label="",
            since="2026-10-12 09:00 UTC",
        )
    ]
    out = _render(running=pin, expected=pin, skew=skew)
    assert "Work needed" in out
    assert "limits differ from the service tier" in out
    assert "since 2026-10-12 09:00 UTC" in out
    assert "/do-acknowledge" not in out
    section = out[out.index('id="work"') : out.index('id="requests"')]
    assert "<form" not in section
    assert 'id="work"' not in _render(running=pin, expected=pin)


def test_work_with_a_way_to_do_it_has_a_button_that_does_it():
    pin = pinned_litellm_version()
    skew = [
        edict(
            what="The free tier project allows any model.",
            subject="llmao:project:free-tier",
            action="/do-restrict-project",
            action_label="Restrict",
            since="",
        )
    ]
    out = _render(running=pin, expected=pin, skew=skew)
    assert 'action="/do-restrict-project"' in out
    assert 'name="subject" value="llmao:project:free-tier"' in out
    assert ">Restrict</button>" in out
    # the identifier is for the form; the sentence is what a person reads
    assert out.count("llmao:project:free-tier") == 1


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


def _pending(**over):
    row = edict(
        id="r1",
        what="key",
        requester="jdoe",
        project="",
        reason="for the hadoop CI",
        wanted_h="hadoop CI",
        tier="service",
        is_capacity=ezt.boolean(False),
        means='Lets jdoe create one service key, "hadoop CI". Nothing is created until they do; they have 14 days.',
        created_at="2026-10-19",
    )
    row.update(over)
    return row


def test_the_count_waiting_is_said_outright_not_hidden_in_a_badge():
    pin = pinned_litellm_version()
    out = _render(running=pin, expected=pin, requests=[_pending(), _pending(id="r2")])
    assert "2 requests awaiting a decision" in out
    assert 'class="card h-100 admin-tile admin-tile-hot"' in out
    one = _render(running=pin, expected=pin, requests=[_pending()])
    assert "1 request awaiting a decision" in one
    none = _render(running=pin, expected=pin)
    assert "Nothing waiting." in none
    assert "admin-tile-hot" not in none


def test_each_request_says_what_approving_it_does():
    pin = pinned_litellm_version()
    out = _render(running=pin, expected=pin, requests=[_pending()])
    assert "If you approve:" in out
    assert "Lets jdoe create one service key" in out
    assert 'name="request_id" value="r1"' in out


def test_approvals_not_yet_used_say_who_may_do_what_until_when():
    pin = pinned_litellm_version()
    row = edict(
        requester="jdoe",
        ask='one service key, "hadoop CI"',
        decided_by="admin1",
        decided_on="2026-10-19",
        expires_h="on 2026-11-02",
        soon=ezt.boolean(False),
    )
    out = _render(running=pin, expected=pin, approvals=[row])
    assert "may create one service key" in out
    assert "approved by admin1 on 2026-10-19" in out
    assert "lapses on 2026-11-02" in out
    assert "None outstanding." in _render(running=pin, expected=pin)


def test_recent_work_reads_as_sentences_with_who_and_when():
    pin = pinned_litellm_version()
    row = edict(
        at="2026-10-19T14:02:11",
        by="admin1",
        sentence="Restricted free-tier to our own boxes.",
        icon="bi-shield-lock",
        ago="5m ago",
    )
    out = _render(running=pin, expected=pin, recent=[row])
    assert "Restricted free-tier to our own boxes." in out
    assert "admin1 &middot;" in out
    assert "5m ago" in out
    assert "Nothing in the last 14 days." in _render(running=pin, expected=pin)

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
        admin_at_heart=ezt.boolean(True),
        as_user=ezt.boolean(False),
        nav="admin",
        admin_nav="admin",
        login_back="/admin",
        restarted_at="2026-10-08 14:30 UTC",
        restarted_ago="3d ago",
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


def test_capacity_page_renders_demand_beside_the_rentals():
    """The template with one model's demand, from rows shaped like LiteLLM's, must render.

    A template error would otherwise show up as a 500 on /admin/capacity.
    """
    import datetime as dt

    import asfquart

    from llmao.capacity import fold_demand
    from llmao.planning import Recommendation, summarise_demand

    if asfquart.APP is None:
        asfquart.construct(
            "llmao", app_dir=str(THIS_DIR), cfg_file="config.yaml.example", oauth=False, force_login=False
        )
    import pages

    day = dt.date(2026, 11, 1)
    spend = [
        edict(
            startTime=f"{day}T09:10:00.000+00:00",
            model_group="gemma4-26b",
            status=status,
            prompt_tokens=580,
            completion_tokens=46,
            request_duration_ms=600,
            metadata=edict(user_api_key_team_alias="spark", user_agent="OpenAI/Python 1.109.1", error_information=err),
        )
        for status, err in (("success", None), ("failure", edict(normalized_error="429_RATE_LIMIT_EXCEEDED")))
    ]
    summary = summarise_demand(fold_demand(day, spend), today=day, period_days=7)
    cards, dormant = pages.capacity_cards(
        [], summary, catalog={"gemma4-26b": "Gemma 4 26B"}, live={"gemma4-26b"}, today=day, period_days=7
    )
    data = edict(
        title="Capacity",
        is_site_admin=ezt.boolean(True),
        admin_at_heart=ezt.boolean(True),
        as_user=ezt.boolean(False),
        nav="admin",
        admin_nav="capacity",
        login_back="/admin/capacity",
        restarted_at="",
        restarted_ago="",
        reveal_supply=False,
        uid="u@example.apache.org",
        name="N",
        can_create_automation=ezt.boolean(False),
        projects=[],
        projects_label=None,
        admin_projects=[],
        flashes=[],
        notices=[],
        work=None,
        repo="https://github.com/apache/tooling-llmao",
        commit="deadbeef",
        period_days=7,
        periods=[edict(days=7, active=ezt.boolean(True))],
        fleet_cost_per_day="$0.00",
        fleet_tokens_per_day="0",
        spend_month_to_date="$0.00",
        spend_projected="$0.00",
        unpriced_hours=None,
        capacity_down=ezt.boolean(False),
        demand_down=ezt.boolean(False),
        cards=cards,
        dormant=dormant,
        recommendations=[],
        spend_down=ezt.boolean(False),
        providers=[],
    )
    template = ezt.Template(
        str(THIS_DIR / "templates" / "capacity.ezt"), compress_whitespace=0, base_format=ezt.FORMAT_HTML
    )

    def render() -> str:
        buf = io.StringIO()
        template.generate(buf, data)
        return buf.getvalue()

    out = render()
    # Demand with no hourly rows yet: the card has only the asked half.
    assert "What was asked of it" in out and "What it served" not in out
    assert "Gemma 4 26B" in out and ">gemma4-26b</code>" in out
    assert "nothing serving it now" not in out
    assert "2 requests/day" in out
    assert "spark" in out and "OpenAI" in out
    assert "429_RATE_LIMIT_EXCEEDED" in out
    assert "text-danger" in out  # half the requests refused is a warning
    assert "0.6s typical" in out
    # The day's requests, charted over the whole week, the six empty days as zero.
    assert 'data-chart="{&quot;days&quot;: [&quot;2026-10-26&quot;' in out
    assert "&quot;values&quot;: [0, 0, 0, 0, 0, 0, 2]" in out
    assert "/static/js/echarts.min.js" in out
    # Nothing to recommend is said, not left blank; a recommendation is its sentences and a link to the card.
    assert "Nothing to change" in out and 'id="cap-gemma4-26b"' in out
    data.recommendations = [
        pages._recommendation_row(
            Recommendation(
                rule="replace_cache_bound",
                model="gemma4-26b",
                rental="Vast 1",
                verb="Replace",
                headline="Replace Vast 1:8001 (RTX 3060 x2).",
                evidence=("It ran out of cache.", "It was idle otherwise."),
                action=("Rent a bigger card.", "Return the old one."),
            )
        )
    ]
    out = render()
    assert "Nothing to change" not in out
    assert ">Replace<" in out and "Replace Vast 1:8001 (RTX 3060 x2)." in out
    assert "It ran out of cache. It was idle otherwise." in out
    assert "<li>Rent a bigger card.</li>" in out and 'href="#cap-gemma4-26b"' in out
    data.recommendations = []
    # Nothing bought from a model provider in the period: no section and no tiles for it.
    assert "Providers we pay" not in out
    assert "month to date" not in out
    data.providers = [
        pages._spend_provider_row(
            edict(
                provider="xAI",
                period_usd=1.0,
                today_usd=0.0,
                month_to_date_usd=1.0,
                projected_month_usd=3.0,
                models=[],
                projects=[],
                daily=[],
            )
        )
    ]
    out = render()
    assert "Providers we pay" in out
    assert "Providers, month to date" in out
    # And a model that never refused a request has no Refused column.
    data.cards[0].demand.refused_any = ezt.boolean(False)
    assert "Refused" not in render()
    # A model's rentals: named, with when each was seen and what became of it.
    line = edict(
        server="Vast 9:8001",
        rental="Vast 9",
        label="RTX 3060 x2",
        addresses=["203.0.113.10:8001"],
        first_hour="2026-10-19T08",
        last_hour="2026-10-20T18",
        hours=35,
        usd_per_hour=0.103,
        busy=0.1,
        waiting=0.0,
        kv_peak=0.2,
        preemptions=0,
        tokens=514,
    )
    model = edict(
        model_name="qwen3-8b-bf16",
        cost_usd=3.6,
        last_hour="2026-10-20T18",
        cost_per_day=2.4,
        tokens_per_day=250,
        usd_per_mtok=None,
        busy=0.1,
        waiting=0.0,
        kv_peak=0.2,
        waiting_peak=0,
        preemptions=0,
        queue_p95_worst_s=None,
        prompt_tokens=500,
        generation_tokens=14,
        requests=8,
        hours=35,
        unpriced_hours=0,
        daily_tokens=[],
        servers=[line],
    )
    # Nothing serves it now and nothing was seen for a week: a footnote, not a card.
    data.cards, data.dormant = pages.capacity_cards(
        [model], [], catalog={}, live=set(), today=day, period_days=7, listed={}
    )
    assert data.cards == [] and [c.model_name for c in data.dormant] == ["qwen3-8b-bf16"]
    out = render()
    assert "last seen Oct 20 ($3.60 over 35 hours)" in out
    assert "Vast 9:8001" not in out
    # Still deployed: a card, with its rentals.
    data.cards, data.dormant = pages.capacity_cards(
        [model], [], catalog={}, live={"qwen3-8b-bf16"}, today=day, period_days=7, listed={}
    )
    out = render()
    assert "What it served" in out and "What was asked of it" not in out
    assert "Vast 9:8001" in out and "RTX 3060 x2" in out
    assert "Oct 19 to Oct 20" in out and ">ended<" in out
    assert "hours sampled" not in out
    # No rental preempted anything: no column. One did: the column, with the count on its line.
    assert "Preempted" not in out
    model.servers[0].preemptions = 7  # edict copied the line on construction
    data.cards, data.dormant = pages.capacity_cards(
        [model], [], catalog={}, live={"qwen3-8b-bf16"}, today=day, period_days=7, listed={}
    )
    out = render()
    assert "Preempted" in out and 'text-warning-emphasis">7</td>' in out


def test_admin_drops_models_and_shows_a_version_match():
    pin = pinned_litellm_version()
    out = _render(running=pin, expected=pin)
    assert "Routes, hosts and serving state" not in out
    assert f"{pin}, matches the pin." in out


def test_admin_pages_share_one_nav_with_the_current_page_marked():
    """The four admin pages are reached from pills under the heading, not cards at the foot."""
    out = _render(running="1.0.0", expected="1.0.0")
    assert "Settings and lists" not in out
    for href in ("/admin", "/admin/capacity", "/admin/tiers", "/keys/other"):
        assert f'href="{href}"' in out
    assert 'class="nav-link active" href="/admin" aria-current="page"' in out
    assert 'class="nav-link" href="/admin/tiers"' in out


def test_admin_tiles_run_in_the_order_of_the_sections_they_open():
    """Work needed is the first section, so it is the first tile."""
    out = _render(running="1.0.0", expected="1.0.0")
    tiles = [out.index(f'href="#{anchor}" class="card') for anchor in ("work", "requests", "approvals", "litellm")]
    assert tiles == sorted(tiles)


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

    Shown as the same kind of rental, one would vanish and the other would not,
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
        created_at="2026-10-19 09:00 UTC",
        created_ago="3d ago",
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
        sentence="Restricted free-tier to our own rentals.",
        icon="bi-shield-lock",
        ago="5m ago",
    )
    out = _render(running=pin, expected=pin, recent=[row])
    assert "Restricted free-tier to our own rentals." in out
    assert "admin1 &middot;" in out
    assert "5m ago" in out
    assert "Nothing in the last 14 days." in _render(running=pin, expected=pin)

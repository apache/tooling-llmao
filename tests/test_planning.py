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
"""The summaries read for /admin/capacity (llmao/planning.py)."""

from __future__ import annotations

import datetime as dt

from easydict import EasyDict as edict  # noqa: N813

from llmao.capacity import fold_demand
from llmao.models import VllmRecipe
from llmao.planning import Advisor, days_to_read, summarise_capacity, summarise_demand, summarise_spend
from tests.test_capacity import DAY, _spend

SPEND_TODAY = dt.date(2026, 10, 19)


def _row(day, provider, model, project, usd):
    return edict(day=day, provider=provider, model_name=model, project=project, spend_usd=usd, tokens=0, requests=0)


def test_enough_days_are_read_for_the_month_to_date():
    assert days_to_read(7, SPEND_TODAY) == 19
    assert days_to_read(30, SPEND_TODAY) == 30
    assert days_to_read(7, dt.date(2026, 10, 3)) == 7


def test_a_provider_is_totalled_over_the_period_today_and_the_month():
    rows = [
        _row("2026-09-30", "xai", "grok-4", "tooling", 9.0),  # last month: not in the month to date
        _row("2026-10-01", "xai", "grok-4", "tooling", 4.0),
        _row("2026-10-12", "xai", "grok-4", "mahout", 2.0),  # in the month, outside a 7-day period
        _row("2026-10-18", "xai", "grok-4", "tooling", 3.0),
        _row("2026-10-19", "xai", "grok-4-mini", None, 1.0),
    ]
    (xai,) = summarise_spend(rows, today=SPEND_TODAY, period_days=7)
    assert xai.provider == "xai"
    assert xai.period_usd == 4.0
    assert xai.today_usd == 1.0
    assert xai.month_to_date_usd == 10.0
    # 10 over 19 days, for a 31-day month
    assert xai.projected_month_usd == round(10.0 / 19 * 31, 2)
    assert [(m.name, m.usd) for m in xai.models] == [("grok-4", 3.0), ("grok-4-mini", 1.0)]
    assert [(p.name, p.usd) for p in xai.projects] == [("tooling", 3.0), ("(no project)", 1.0)]
    assert [(d.day, d.usd) for d in xai.daily] == [("2026-10-18", 3.0), ("2026-10-19", 1.0)]


def test_providers_are_listed_by_spend_largest_first():
    rows = [
        _row("2026-10-19", "openrouter", "gemma4-26b", "tooling", 0.5),
        _row("2026-10-19", "xai", "grok-4", "x", 2.0),
    ]
    assert [p.provider for p in summarise_spend(rows, today=SPEND_TODAY, period_days=7)] == ["xai", "openrouter"]


def test_nothing_bought_is_nothing_to_show():
    assert summarise_spend([], today=SPEND_TODAY, period_days=30) == []


# ---- the self-hosted report ----


def _hour_row(
    hour,
    server,
    model,
    *,
    usd=0.1,
    covered=3600,
    busy=0.5,
    waiting=0.0,
    kv=0.2,
    prompt=1000,
    gen=100,
    q95=None,
    rental="",
    rental_label="",
    preemptions=0,
):
    return edict(
        hour=hour,
        server=server,
        model_name=model,
        rental=rental,
        rental_label=rental_label,
        covered_s=covered,
        busy_frac=busy,
        waiting_frac=waiting,
        waiting_peak=1 if waiting else 0,
        kv_avg=kv / 2,
        kv_peak=kv,
        prompt_tokens=prompt,
        generation_tokens=gen,
        requests=10,
        preemptions=preemptions,
        queue_avg_s=None,
        queue_p95_s=q95,
        usd_per_hour=usd,
    )


def test_a_model_is_costed_by_the_hour_and_weighed_by_what_was_sampled():
    rows = [
        _hour_row("2026-10-19T08", "203.0.113.10:8001", "qwen3-8b", usd=0.5, covered=900, busy=1.0, q95=0.3),
        _hour_row("2026-10-19T09", "203.0.113.10:8001", "qwen3-8b", usd=0.5, covered=2700, busy=0.0, q95=2.5),
        _hour_row("2026-10-18T09", "198.51.100.8:8001", "qwen3-8b", usd=0.25, kv=0.9),
        _hour_row("2026-10-19T09", "192.0.2.4:8001", "gemma4-26b", usd=1.0),
        # outside a 7-day period ending 2026-10-19
        _hour_row("2026-10-12T09", "198.51.100.8:8001", "qwen3-8b", usd=99.0),
    ]
    report = summarise_capacity(rows, today=dt.date(2026, 10, 19), period_days=7)
    _gemma, qwen = sorted(report.models, key=lambda m: m.model_name)
    # A whole hour of rental for every hour sampled, however much of it was.
    assert qwen.cost_usd == 1.25
    # Over the three clock hours seen, as a rate per day.
    assert qwen.cost_per_day == 1.25 / 3 * 24
    assert qwen.hours == 3
    # Busy over 900s + 2700s + 3600s sampled: (900*1.0 + 3600*0.5) / 7200.
    assert qwen.busy == (900 * 1.0 + 3600 * 0.5) / 7200
    assert qwen.kv_peak == 0.9
    assert qwen.queue_p95_worst_s == 2.5
    assert qwen.usd_per_mtok == 1.25 / (3 * 1100 / 1e6)
    assert [(d.day, d.value) for d in qwen.daily_tokens] == [("2026-10-18", 1100), ("2026-10-19", 2200)]
    # Unstamped rows: a line per address, named by it, first seen first.
    assert [s.server for s in qwen.servers] == ["198.51.100.8:8001", "203.0.113.10:8001"]
    assert (qwen.servers[1].first_hour, qwen.servers[1].last_hour) == ("2026-10-19T08", "2026-10-19T09")
    # The costliest model first; the fleet's day is every model's.
    assert [m.model_name for m in report.models] == ["qwen3-8b", "gemma4-26b"]
    assert report.cost_usd == 2.25
    assert report.hours_seen == 3
    assert report.cost_per_day == 2.25 / 3 * 24


def test_hours_over_a_threshold_are_counted_so_a_burst_is_not_lost_in_the_average():
    """A model busy 1% of the period can still have run flat out for hours; the count says so."""
    quiet = "203.0.113.10:8001"
    rows = [_hour_row(f"2026-10-19T{h:02d}", quiet, "m", busy=0.0) for h in range(20)]
    rows += [
        _hour_row("2026-10-19T20", quiet, "m", busy=0.97, waiting=0.67, kv=1.0, preemptions=10),
        _hour_row("2026-10-19T21", quiet, "m", busy=0.5, waiting=0.04, kv=0.96),
        _hour_row("2026-10-19T22", quiet, "m", busy=0.49, waiting=0.05, kv=0.9, preemptions=1),
        # Another rental the same hour, never short of room.
        _hour_row("2026-10-19T20", "198.51.100.8:8001", "m", busy=0.69, waiting=0.01, kv=0.76),
    ]
    (m,) = summarise_capacity(rows, today=dt.date(2026, 10, 19), period_days=7).models
    assert m.busy < 0.12  # the average says idle
    assert (m.hours_busy, m.hours_queued, m.hours_cache_full, m.hours_preempting) == (3, 2, 2, 2)
    busy_one, other = m.servers  # first seen first: the quiet one from midnight, the other at 20:00
    assert (busy_one.hours_busy, busy_one.hours_queued, busy_one.hours_cache_full, busy_one.hours_preempting) == (
        2,
        2,
        2,
        2,
    )
    assert (other.hours_busy, other.hours_queued, other.hours_cache_full, other.hours_preempting) == (1, 0, 0, 0)


def test_a_rentals_hours_are_one_line_across_an_address_change_and_from_before_the_stamp():
    def stamped(hour, server, **kw):
        return _hour_row(hour, server, "m", rental="Vast 1", rental_label="RTX 3060 x2", **kw)

    rows = [
        # Before the stamp, the same server as the stamped rows that follow.
        _hour_row("2026-10-19T07", "203.0.113.10:8001", "m", usd=0.5),
        stamped("2026-10-19T08", "203.0.113.10:8001", usd=0.5, preemptions=4),
        # The provider moved the machine: a new address, the same rental and port.
        stamped("2026-10-19T09", "198.51.100.8:8001", usd=0.6, preemptions=6),
        # A second process on the same rental is its own line.
        stamped("2026-10-19T09", "198.51.100.8:8002", usd=0.6),
        # A server never stamped is a line of its own, by its address.
        _hour_row("2026-10-19T09", "192.0.2.4:8001", "m"),
    ]
    (m,) = summarise_capacity(rows, today=dt.date(2026, 10, 19), period_days=7).models
    # First seen first; the two seen in the same hour by name.
    assert [s.server for s in m.servers] == ["Vast 1:8001", "192.0.2.4:8001", "Vast 1:8002"]
    moved, loose, second = m.servers
    assert moved.rental == "Vast 1" and moved.label == "RTX 3060 x2"
    assert moved.addresses == ["198.51.100.8:8001", "203.0.113.10:8001"]
    assert (moved.first_hour, moved.last_hour, moved.hours) == ("2026-10-19T07", "2026-10-19T09", 3)
    # The last price seen: the machine's price where it is now.
    assert moved.usd_per_hour == 0.6
    # Preemptions are kept per line: which rental ran out of cache.
    assert (moved.preemptions, second.preemptions) == (10, 0)
    assert second.hours == 1
    assert loose.rental == "" and loose.addresses == ["192.0.2.4:8001"]


def test_an_unpriced_hour_is_counted_as_unpriced_not_as_free():
    rows = [
        _hour_row("2026-10-19T08", "203.0.113.10:8001", "m", usd=None),
        _hour_row("2026-10-19T09", "203.0.113.10:8001", "m", usd=0.5),
    ]
    (m,) = summarise_capacity(rows, today=dt.date(2026, 10, 19), period_days=7).models
    assert m.unpriced_hours == 1
    assert m.cost_usd == 0.5
    # The cost per token is over the priced hour alone: its rental against
    # its tokens, so the unpriced hour neither hides the figure nor dilutes it.
    assert m.usd_per_mtok == 0.5 / (1100 / 1e6)


def test_no_cost_per_token_when_nothing_priced_served_anything():
    rows = [
        _hour_row("2026-10-19T08", "203.0.113.10:8001", "m", usd=None),
        _hour_row("2026-10-19T09", "203.0.113.10:8001", "m", usd=0.5, prompt=0, gen=0),
    ]
    (m,) = summarise_capacity(rows, today=dt.date(2026, 10, 19), period_days=7).models
    assert m.usd_per_mtok is None


def test_no_rows_is_an_empty_report():
    report = summarise_capacity([], today=dt.date(2026, 10, 19), period_days=30)
    assert report.models == []
    assert report.cost_per_day == 0.0


def test_demand_is_summarised_per_model_over_the_period():
    rows = fold_demand(DAY, [_spend(9), _spend(9, failure="429_RATE_LIMIT"), _spend(10, agent="codex-tui/1")])
    (qwen_row,) = fold_demand(DAY, [_spend(9, model="qwen3.8-27b")])
    qwen_row.hour = "2026-11-02T09"  # the next day
    (gemma, qwen) = summarise_demand([*rows, qwen_row], today=dt.date(2026, 11, 2), period_days=7)
    assert gemma.model == "gemma4-26b"
    assert gemma.requests == 3
    assert gemma.days == 1
    assert gemma.requests_per_day == 3
    assert gemma.refused_total == 1
    assert gemma.refused == [edict(name="429_RATE_LIMIT", count=1)]
    assert [t.name for t in gemma.by_tool] == ["OpenAI", "codex-tui"]
    assert gemma.by_project[0].name == "spark"
    assert [b.count for b in gemma.prompt_len] == [2, 0, 0, 0, 0]
    # A demand value from before fold version 3: the project's share has
    # only its counts. Those still count; the rest waits for the re-roll.
    old = edict(rows[0], by_project={"spark": dict(requests=2, prompt_tokens=580, completion_tokens=46)})
    (gemma_old,) = summarise_demand([old], today=dt.date(2026, 11, 2), period_days=7)
    assert (gemma_old.requests, gemma_old.by_project[0].requests) == (2, 2)
    assert (gemma_old.by_tool, gemma_old.refused) == ([], [])
    assert gemma.duration_p50_s == 0.6
    assert gemma.daily_requests == [edict(day="2026-11-01", value=3)]
    assert qwen.requests == 1
    # A period that excludes the days leaves nothing.
    assert summarise_demand([*rows, qwen_row], today=dt.date(2026, 11, 20), period_days=7) == []


# ---- recommendations ----


def _qwen_rows():
    """The qwen3-8b shape of October: a cheap pair of 12GB cards that ran out of cache, a bigger rental that never did."""

    def small(hour, **kw):
        return _hour_row(hour, "203.0.113.10:8001", "qwen3-8b", rental="Vast 1", rental_label="RTX 3060 x2", **kw)

    def big(hour, **kw):
        return _hour_row(hour, "198.51.100.8:8003", "qwen3-8b", rental="RunPod p", rental_label="llmao-qwen3-8b", **kw)

    rows = [small(f"2026-10-19T{h:02d}", busy=0.0) for h in range(20)]
    rows += [big(f"2026-10-19T{h:02d}", busy=0.0) for h in range(20)]
    rows += [
        small("2026-10-19T20", busy=0.97, waiting=0.67, kv=1.0, preemptions=10, q95=120.0),
        big("2026-10-19T20", busy=0.69, waiting=0.01, kv=0.76, q95=2.0),
    ]
    return rows


QWEN_RECIPE = VllmRecipe(
    hf_model="Qwen/Qwen3-8B-FP8", served_name="qwen3-8b", vram_gb=9, max_model_len=40960, kv_bytes_per_token=73728
)


def _advisor(rows, *, current=frozenset({"Vast 1", "RunPod p"}), recipe=QWEN_RECIPE, demand=()):
    supply = summarise_capacity(rows, today=dt.date(2026, 10, 19), period_days=7)
    return Advisor(
        supply=supply, demand=list(demand), recipes={"qwen3-8b": recipe}, current=set(current), period_days=7
    )


def test_a_rental_that_ran_out_of_cache_is_to_be_replaced_with_more_memory_on_one_card():
    demand = summarise_demand(fold_demand(DAY, [_spend(9, model="qwen3-8b", prompt=10_000)]), today=DAY, period_days=7)
    (rec,) = _advisor(_qwen_rows(), demand=demand).advise()
    assert (rec.verb, rec.model, rec.rental, rec.key) == (
        "Replace",
        "qwen3-8b",
        "Vast 1",
        "replace_cache_bound:qwen3-8b:Vast 1",
    )
    assert rec.headline == (
        "Replace Vast 1:8001 (RTX 3060 x2), serving qwen3-8b, with a rental that has more memory on one card."
    )
    # Every sentence stands alone, with its figure in it.
    assert rec.evidence == (
        "Over the last 7 days it ran out of KV cache in 1 hour and evicted 10 running requests, "
        "which were started again later.",
        "In the worst hour, requests waited up to 120s.",
        "The other rental, RunPod p:8003 (llmao-qwen3-8b), never ran out of cache in the same period.",
        "The model was busy 4% of the time, so this is the card's memory, not throughput.",
        "Most prompts were 8k-32k tokens long.",
    )
    assert rec.action == (
        "Rent one card with room for qwen3-8b and several prompts at once. Its weights need 9 GB, "
        "and a full 40,960-token context another 2.8 GB of cache.",
        "Once the new rental serves qwen3-8b, return Vast 1:8001 (RTX 3060 x2).",
    )
    assert rec.saving_usd_per_day == 0.0


def test_what_the_replace_rule_cannot_say_it_leaves_out():
    # No demand rows and a recipe that declares nothing: no prompt-length sentence, a plainer step.
    (rec,) = _advisor(_qwen_rows(), recipe=VllmRecipe(hf_model="x", served_name="qwen3-8b")).advise()
    assert not any("prompts were" in s for s in rec.evidence)
    assert (
        rec.action[0] == "Rent one card with more memory for qwen3-8b; its recipe does not declare how much it needs."
    )
    # The cheap pair is gone already: nothing to replace.
    assert _advisor(_qwen_rows(), current={"RunPod p"}).advise() == []


def test_a_fleet_that_never_ran_short_gets_no_advice():
    rows = [_hour_row("2026-10-19T08", "203.0.113.10:8001", "qwen3-8b", rental="Vast 1", busy=0.3, kv=0.5)]
    assert _advisor(rows).advise() == []


def test_the_log_only_fields_are_summed_and_absent_rows_from_before_the_log_count_as_nothing():
    rows = fold_demand(DAY, [_spend(9), _spend(10)])
    rows[0].update(log_requests=1, streamed=1, with_tools=0, finish_length=1, ttft_p50_s=0.4, ttft_p95_s=2.0)
    # rows[1] was written before the log existed: no log fields at all.
    (m,) = summarise_demand(rows, today=DAY, period_days=7)
    assert (m.log_requests, m.streamed, m.with_tools, m.finish_length) == (1, 1, 0, 1)
    assert (m.ttft_p50_s, m.ttft_p95_worst_s) == (0.4, 2.0)
    (quiet,) = summarise_demand(fold_demand(DAY, [_spend(9)]), today=DAY, period_days=7)
    assert quiet.log_requests == 0 and quiet.ttft_p50_s is None

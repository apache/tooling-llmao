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

"""What the capacity rows say, read for planning.

The summaries behind /admin/capacity: each model over a period from the
hourly capacity rows (summarise_capacity, with one line per rental and
listen port from _server_lines), from the demand rows (summarise_demand),
and the totals of provider spend (summarise_spend). The rows themselves,
how they are measured, folded, stored and pruned, are llmao/capacity.py;
this module only reads them. See docs/capacity-design.md.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass

from easydict import EasyDict as edict  # noqa: N813

from llmao import fmt
from llmao.capacity import add_counts
from llmao.prompt_log import NO_PROJECT, PROMPT_BUCKETS, percentile


def _ranked_counts(totals: dict[str, dict]) -> list[edict]:
    """[{name, requests, prompt_tokens, completion_tokens}], most requests first."""
    return [edict(name=name, **v) for name, v in sorted(totals.items(), key=lambda kv: -kv[1]["requests"])]


def summarise_demand(rows: list, *, today: dt.date, period_days: int) -> list[edict]:
    """Each model over the last `period_days` days, from demand values; most requests first.

    Rates per day are over the days that have demand values, so a period
    that starts before the rollup did is not averaged over empty days.
    Tools, refusals and prompt lengths are summed over the projects'
    shares, where the demand value keeps them. A share from before fold
    version 3 lacks them, until its day is rolled up again.
    """
    start = (today - dt.timedelta(days=period_days - 1)).isoformat()
    rows = [r for r in rows if r.hour[:10] >= start]
    models = []
    for name in sorted({r.model for r in rows}):
        mine = [r for r in rows if r.model == name]
        days = len({r.hour[:10] for r in mine})
        requests = sum(r.requests for r in mine)
        by_project: dict[str, dict] = {}
        by_tool: dict[str, dict] = {}
        refused: dict[str, int] = defaultdict(int)
        prompt_len: dict[str, int] = defaultdict(int)
        daily: dict[str, int] = defaultdict(int)
        for r in mine:
            daily[r.hour[:10]] += r.requests
            for who, share in r.by_project.items():
                add_counts(
                    by_project,
                    who,
                    requests=share.requests,
                    prompt_tokens=share.prompt_tokens,
                    completion_tokens=share.completion_tokens,
                )
                for tool, v in (share.get("by_tool") or {}).items():
                    add_counts(by_tool, tool, **v)
                for why, n in (share.get("refused") or {}).items():
                    refused[why] += n
                for bucket, n in (share.get("prompt_len") or {}).items():
                    prompt_len[bucket] += n
        p50s = [r.duration_p50_s for r in mine if r.duration_p50_s is not None]
        p95s = [r.duration_p95_s for r in mine if r.duration_p95_s is not None]
        # What only the prompt log says; absent from rows written before it existed.
        ttft_p50s = [r.ttft_p50_s for r in mine if r.get("ttft_p50_s") is not None]
        ttft_p95s = [r.ttft_p95_s for r in mine if r.get("ttft_p95_s") is not None]
        models.append(
            edict(
                model=name,
                days=days,
                last_hour=max(r.hour for r in mine),
                requests=requests,
                requests_per_day=requests / days if days else 0.0,
                prompt_tokens=sum(r.prompt_tokens for r in mine),
                completion_tokens=sum(r.completion_tokens for r in mine),
                refused_total=sum(refused.values()),
                refused=[edict(name=k, count=v) for k, v in sorted(refused.items(), key=lambda kv: -kv[1])],
                by_project=_ranked_counts(by_project),
                by_tool=_ranked_counts(by_tool),
                prompt_len=[edict(name=label, count=prompt_len.get(label, 0)) for _limit, label in PROMPT_BUCKETS],
                # The median of the hourly medians: a typical hour. The worst
                # p95 says how bad the slowest hour got.
                duration_p50_s=percentile(p50s, 0.5),
                duration_p95_worst_s=max(p95s) if p95s else None,
                daily_requests=[edict(day=d, value=daily[d]) for d in sorted(daily)],
                log_requests=sum(r.get("log_requests") or 0 for r in mine),
                streamed=sum(r.get("streamed") or 0 for r in mine),
                with_tools=sum(r.get("with_tools") or 0 for r in mine),
                finish_length=sum(r.get("finish_length") or 0 for r in mine),
                ttft_p50_s=percentile(ttft_p50s, 0.5),
                ttft_p95_worst_s=max(ttft_p95s) if ttft_p95s else None,
            )
        )
    return sorted(models, key=lambda m: -m.requests)


# ---- the self-hosted report ----------------------------------------------
#
# Totals of the hourly rows, per model, for /admin/capacity.
#
# Cost counts each row as one hour of rental: the box is billed for the whole
# hour whether or not llmao sampled all of it. Tokens, busy and waiting are
# what was sampled; busy and waiting are weighted by how much of each hour
# was. An hour with no row at all (llmao down throughout) is missing from
# both, which is the gap the design accepts.


def _weighted(rows: list, field_name: str) -> float:
    """A fraction averaged over rows, weighted by how much of each hour was sampled."""
    covered = sum(r.covered_s for r in rows)
    return sum(r[field_name] * r.covered_s for r in rows) / covered if covered else 0.0


# An hour counts as busy when a request was running for at least half of
# it, as queued when one waited for at least a twentieth of it (three
# minutes), and as cache-full when the KV cache reached 95%. Averages over
# a period hide bursts: a model busy 1% of the month can still have run
# flat out for ten hours of it, and those hours are what a decision to add
# or replace a rental turns on.
BUSY_HOUR = 0.5
QUEUED_HOUR = 0.05
CACHE_FULL_HOUR = 0.95


def _hot_hours(rows: list) -> dict:
    """How many of the rows' hours were busy, queued, cache-full, or preempting."""
    return dict(
        hours_busy=sum(1 for r in rows if r.busy_frac >= BUSY_HOUR),
        hours_queued=sum(1 for r in rows if r.waiting_frac >= QUEUED_HOUR),
        hours_cache_full=sum(1 for r in rows if r.kv_peak >= CACHE_FULL_HOUR),
        hours_preempting=sum(1 for r in rows if r.preemptions),
    )


def _port_of(server: str) -> str:
    return server.rsplit(":", 1)[-1]


def _server_lines(rows: list) -> list[edict]:
    """One line per vLLM process over the rows, named by its rental where the rows say which.

    A process is a rental and a listen port. Its rows are keyed by address
    and port, so a process whose address changed sits under two servers;
    here those are one line. Rows from before the stamp, which name only
    the server, join the line that server was later stamped onto, and a
    server never stamped is a line of its own, named by its address.
    First to last hour seen, so a line that stopped reads as having
    stopped.
    """
    stamped = {r.server: (r.rental, _port_of(r.server)) for r in rows if r.get("rental")}

    def line_of(r) -> tuple[str, str]:
        if r.get("rental"):
            return (r.rental, _port_of(r.server))
        return stamped.get(r.server) or ("", r.server)

    by_line: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        by_line[line_of(r)].append(r)
    lines = []
    for (rental, port), theirs in by_line.items():
        hours = sorted(r.hour for r in theirs)
        queue = [r.queue_p95_s for r in theirs if r.queue_p95_s is not None]
        lines.append(
            edict(
                server=f"{rental}:{port}" if rental else port,
                rental=rental,
                label=next((r.rental_label for r in theirs if r.get("rental_label")), ""),
                addresses=sorted({r.server for r in theirs}),
                first_hour=hours[0],
                last_hour=hours[-1],
                hours=len(theirs),
                usd_per_hour=next((r.usd_per_hour for r in reversed(theirs) if r.usd_per_hour is not None), None),
                busy=_weighted(theirs, "busy_frac"),
                waiting=_weighted(theirs, "waiting_frac"),
                kv_peak=max(r.kv_peak for r in theirs),
                preemptions=sum(r.preemptions for r in theirs),
                queue_p95_worst_s=max(queue) if queue else None,
                tokens=sum(r.prompt_tokens + r.generation_tokens for r in theirs),
                **_hot_hours(theirs),
            )
        )
    return sorted(lines, key=lambda line: (line.first_hour, line.server))


def summarise_capacity(rows: list, *, today: dt.date, period_days: int) -> edict:
    """The fleet and each model over the last `period_days` days, from hourly rows."""
    start = (today - dt.timedelta(days=period_days - 1)).isoformat()
    rows = [r for r in rows if (r.get("hour") or "")[:10] >= start]
    models = []
    for name in sorted({r.model_name for r in rows}):
        mine = [r for r in rows if r.model_name == name]
        priced = [r for r in mine if r.usd_per_hour is not None]
        cost = sum(r.usd_per_hour for r in priced)
        tokens = sum(r.prompt_tokens + r.generation_tokens for r in mine)
        # Rates per day are taken over the clock hours seen, not the days: a
        # day still in progress, or one llmao was down for part of, would
        # otherwise count as a whole day and understate the rate.
        hours_seen = len({r.hour for r in mine})
        daily_tokens: dict[str, int] = defaultdict(int)
        daily_cost: dict[str, float] = defaultdict(float)
        for r in mine:
            daily_tokens[r.hour[:10]] += r.prompt_tokens + r.generation_tokens
            if r.usd_per_hour is not None:
                daily_cost[r.hour[:10]] += r.usd_per_hour
        servers = _server_lines(mine)
        queue = [r.queue_p95_s for r in mine if r.queue_p95_s is not None]
        # The cost of a million tokens: the rental paid in the hours that
        # have a price, divided by the tokens served in those same hours.
        # An hour without a price is left out of both sides. (The first
        # hours after llmao starts have none: the provider's price has not
        # been read yet.) None when no priced hour served a token.
        priced_tokens = sum(r.prompt_tokens + r.generation_tokens for r in priced)
        models.append(
            edict(
                model_name=name,
                servers=servers,
                hours=len(mine),
                last_hour=max(r.hour for r in mine),
                unpriced_hours=len(mine) - len(priced),
                cost_usd=cost,
                cost_per_day=cost / hours_seen * 24,
                prompt_tokens=sum(r.prompt_tokens for r in mine),
                generation_tokens=sum(r.generation_tokens for r in mine),
                tokens_per_day=tokens / hours_seen * 24,
                requests=sum(r.requests for r in mine),
                busy=_weighted(mine, "busy_frac"),
                waiting=_weighted(mine, "waiting_frac"),
                waiting_peak=max(r.waiting_peak for r in mine),
                kv_peak=max(r.kv_peak for r in mine),
                preemptions=sum(r.preemptions for r in mine),
                queue_p95_worst_s=max(queue) if queue else None,
                usd_per_mtok=cost / (priced_tokens / 1e6) if priced_tokens else None,
                daily_tokens=[edict(day=d, value=daily_tokens[d]) for d in sorted(daily_tokens)],
                daily_cost=[edict(day=d, value=round(daily_cost[d], 2)) for d in sorted(daily_cost)],
                **_hot_hours(mine),
            )
        )
    hours_seen = len({r.hour for r in rows})
    cost = sum(m.cost_usd for m in models)
    return edict(
        models=sorted(models, key=lambda m: -m.cost_usd),
        cost_usd=cost,
        cost_per_day=cost / hours_seen * 24 if hours_seen else 0.0,
        tokens_per_day=sum(m.tokens_per_day for m in models),
        unpriced_hours=sum(m.unpriced_hours for m in models),
        hours_seen=hours_seen,
    )


# ---- provider spend -------------------------------------------------------
#
# Totals of what the model providers we pay were paid, over the rows
# UsageManager.provider_spend returns, one per (day, provider, model,
# project). Every figure is LiteLLM's price at the time of each request,
# not the provider's bill: close enough to plan with, not to reconcile.


def days_to_read(period_days: int, today: dt.date) -> int:
    """How many days of rows a summary needs: the period, or the month so far if longer."""
    return max(period_days, today.day)


def _ranked(totals: dict[str, float]) -> list[edict]:
    """[{name, usd}], largest first: the line someone is looking for is the top one."""
    return [edict(name=name, usd=round(usd, 2)) for name, usd in sorted(totals.items(), key=lambda kv: -kv[1])]


def summarise_spend(rows: list, *, today: dt.date, period_days: int) -> list[edict]:
    """One entry per provider, largest spend over the period first.

    Each carries the period's total, today's, the month to date, and a
    projection to month end at the month's daily average so far. Rows must
    cover days_to_read(period_days, today) days.
    """
    start = (today - dt.timedelta(days=period_days - 1)).isoformat()
    month = today.replace(day=1).isoformat()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    out = []
    for provider in sorted({r.provider for r in rows}):
        mine = [r for r in rows if r.provider == provider]
        period = [r for r in mine if r.day >= start]
        to_date = sum(r.spend_usd for r in mine if r.day >= month)
        by_model: dict[str, float] = defaultdict(float)
        by_project: dict[str, float] = defaultdict(float)
        daily: dict[str, float] = defaultdict(float)
        for r in period:
            by_model[r.model_name] += r.spend_usd
            by_project[r.project or NO_PROJECT] += r.spend_usd
            daily[r.day] += r.spend_usd
        out.append(
            edict(
                provider=provider,
                period_usd=round(sum(r.spend_usd for r in period), 2),
                today_usd=round(sum(r.spend_usd for r in mine if r.day == today.isoformat()), 2),
                month_to_date_usd=round(to_date, 2),
                projected_month_usd=round(to_date / today.day * days_in_month, 2),
                models=_ranked(by_model),
                projects=_ranked(by_project),
                daily=[edict(day=d, usd=round(daily[d], 2)) for d in sorted(daily)],
            )
        )
    return sorted(out, key=lambda p: -p.period_usd)


# ---- recommendations ------------------------------------------------------
#
# What to do about what the rows say: the planning answer the page is for.
# Each rule is a method of Advisor, reading the summaries it holds and
# returning Recommendations. The sentence a rule writes, the threshold it
# turns on and the reason for that threshold sit together in the method.
# Every evidence sentence stands alone, with its figure in it, so an admin
# understands without opening a card or a log; no sentence points at one
# that may be absent.


@dataclass(frozen=True)
class Recommendation:
    """One thing to do, with why and how.

    ``rental`` is the rental it names (``Rental.ident``), "" when it is
    about the model as a whole. ``key`` identifies it across days, for
    snoozing: the same rule about the same rental is the same advice.
    """

    rule: str
    model: str
    rental: str
    verb: str
    headline: str
    evidence: tuple[str, ...]
    action: tuple[str, ...]
    saving_usd_per_day: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.rule}:{self.model}:{self.rental}"


def _top_prompt_bucket(demand) -> str:
    """The prompt-length bucket most requests fell in ("8k-32k"), or ""."""
    if demand is None or not demand.requests:
        return ""
    top = max(demand.prompt_len, key=lambda b: b.count)
    return top.name if top.count else ""


class Advisor:
    """The rules, over one period's summaries.

    ``supply`` is summarise_capacity's result, ``demand`` summarise_demand's
    list, ``recipes`` each model's VllmRecipe (None when it has none),
    ``current`` the idents of the rentals a vLLM server of ours runs on
    now: advice about a rental that is gone is noise.
    """

    # In display order: what is broken before what could be better.
    RULES = ("replace_cache_bound",)

    def __init__(self, *, supply, demand: list, recipes: dict, current: set[str], period_days: int):
        self.supply = supply
        self.demand = {d.model: d for d in demand}
        self.recipes = recipes
        self.current = current
        self.period_days = period_days

    def advise(self) -> list[Recommendation]:
        out: list[Recommendation] = []
        for name in self.RULES:
            out.extend(getattr(self, name)())
        return out

    def replace_cache_bound(self) -> list[Recommendation]:
        """A rental whose KV cache ran out: vLLM evicted running requests, or queued them against a full cache.

        A preemption is vLLM giving up on a running request for lack of
        cache and starting it again later; one is enough to say the card
        is too small for the prompts it gets. Queueing in an hour the
        cache was full says the same without an eviction. Either is the
        card's shape, not the fleet's size: the fix is more memory on one
        card, not another rental like it.
        """
        out = []
        for m in self.supply.models:
            lines = [line for line in m.servers if line.rental in self.current]
            for line in lines:
                if not line.preemptions and not (line.hours_cache_full and line.hours_queued):
                    continue
                others = [o for o in lines if o is not line]
                evidence = []
                if line.preemptions:
                    evidence.append(
                        f"Over the last {self.period_days} days it ran out of KV cache in "
                        f"{fmt.plural(line.hours_cache_full, 'hour')} and evicted "
                        f"{fmt.plural(line.preemptions, 'running request')}, which were started again later."
                    )
                else:
                    evidence.append(
                        f"Over the last {self.period_days} days its KV cache was full in "
                        f"{fmt.plural(line.hours_cache_full, 'hour')} and requests queued in "
                        f"{fmt.plural(line.hours_queued, 'hour')}."
                    )
                if line.queue_p95_worst_s is not None and line.queue_p95_worst_s > 0.3:
                    evidence.append(f"In the worst hour, requests waited up to {fmt.seconds(line.queue_p95_worst_s)}.")
                if others and not any(o.preemptions for o in others):
                    for o in others:
                        evidence.append(
                            f"The other rental, {o.server} ({o.label}), never ran out of cache in the same period."
                        )
                evidence.append(
                    f"The model was busy {fmt.pct(m.busy)}% of the time, so this is the card's memory, not throughput."
                )
                bucket = _top_prompt_bucket(self.demand.get(m.model_name))
                if bucket:
                    evidence.append(f"Most prompts were {bucket} tokens long.")
                out.append(
                    Recommendation(
                        rule="replace_cache_bound",
                        model=m.model_name,
                        rental=line.rental,
                        verb="Replace",
                        headline=f"Replace {line.server} ({line.label}), serving {m.model_name}, "
                        "with a rental that has more memory on one card.",
                        evidence=tuple(evidence),
                        action=self._replace_steps(m.model_name, line),
                    )
                )
        return out

    def _replace_steps(self, model: str, line) -> tuple[str, ...]:
        recipe = self.recipes.get(model)
        steps = []
        if recipe is not None and recipe.vram_gb:
            need = f"Its weights need {recipe.vram_gb:g} GB"
            if recipe.kv_bytes_per_token and recipe.max_model_len:
                cache_gb = recipe.kv_bytes_per_token * recipe.max_model_len / 2**30
                need += f", and a full {recipe.max_model_len:,}-token context another {cache_gb:.1f} GB of cache"
            steps.append(f"Rent one card with room for {model} and several prompts at once. {need}.")
        else:
            steps.append(f"Rent one card with more memory for {model}; its recipe does not declare how much it needs.")
        steps.append(f"Once the new rental serves {model}, return {line.server} ({line.label}).")
        return tuple(steps)

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

"""The prompt log: what one record holds, how a day's file is named, how it is read.

A record of every request that flowed through the gateway, one JSON
object per line in one file per UTC day, written by the hook in
litellm_hooks.py and read by bin/llmao-prompt-log and the demand rollup.
Design: docs/prompt-logging-design.md, "The record" and "Files".

This module is imported by the hook inside the proxy process, by the
tool, and by the rollup in llmao/capacity.py, so it imports nothing
heavy: no loguru (the hook owns the sink), nothing of LiteLLM. The
record is built from LiteLLM's StandardLoggingPayload alone, never from
the raw request, so a change in how the proxy parses requests does not
change what is written.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import math
import pathlib
import re
import shutil
import sys
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from easydict import EasyDict as edict  # noqa: N813

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

# One file per UTC day. The sink's pattern is loguru's; the plain name is
# what the reader looks for, then the gzipped one the rotation leaves.
FILE_PREFIX = "prompts-"
FILE_SUFFIX = ".jsonl"
SINK_PATTERN = FILE_PREFIX + "{time:YYYY-MM-DD!UTC}" + FILE_SUFFIX


def day_file_name(day: dt.date) -> str:
    """'prompts-2026-11-01.jsonl'."""
    return f"{FILE_PREFIX}{day.isoformat()}{FILE_SUFFIX}"


def _stamp(epoch: float | None) -> str | None:
    """An epoch as UTC ISO 8601 with milliseconds, 'Z' for the zone."""
    if not epoch:
        return None
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _elapsed(start: float | None, end: float | None) -> float | None:
    if not start or not end:
        return None
    return round(end - start, 3)


def _finish_reason(response: Any) -> str | None:
    """The one word kept from the response: how truncation and tool use show up in aggregate."""
    try:
        return response["choices"][0]["finish_reason"]
    except (KeyError, IndexError, TypeError):
        return None


def _error(info: Mapping | None) -> edict | None:
    """The error's class, code and provider; never the message, which can quote the prompt."""
    if not info:
        return None
    out = edict(code=info.get("error_code"), provider=info.get("llm_provider"), normalized=info.get("normalized_error"))
    out["class"] = info.get("error_class")  # a keyword "class" cannot be written as one
    return out


def record(payload: Mapping) -> edict:
    """One record from LiteLLM's StandardLoggingPayload, the fields in the design's table.

    The prompt is kept (``messages`` as LiteLLM received it, and the
    request's parameters); the response is not, apart from its finish
    reason. Field names are ours.
    """
    meta = payload.get("metadata") or {}
    streamed = bool(payload.get("stream"))
    start, end = payload.get("startTime"), payload.get("endTime")
    return edict(
        ts=_stamp(end),
        duration_s=_elapsed(start, end),
        # LiteLLM sets completionStartTime on every request; only a stream
        # has a first token that arrived before the rest.
        ttft_s=_elapsed(start, payload.get("completionStartTime")) if streamed else None,
        call_id=payload.get("litellm_call_id"),
        trace_id=payload.get("trace_id"),
        call_type=payload.get("call_type"),
        status=payload.get("status"),
        error=_error(payload.get("error_information")),
        model=payload.get("model_group"),
        deployment=payload.get("model"),
        api_base=payload.get("api_base"),
        provider=payload.get("custom_llm_provider"),
        stream=streamed,
        tokens=edict(
            prompt=payload.get("prompt_tokens") or 0,
            completion=payload.get("completion_tokens") or 0,
            total=payload.get("total_tokens") or 0,
        ),
        finish_reason=_finish_reason(payload.get("response")),
        key=edict(
            hash=meta.get("user_api_key_hash"),
            alias=meta.get("user_api_key_alias"),
            user=meta.get("user_api_key_user_id"),
            project=meta.get("user_api_key_team_alias"),
        ),
        end_user=payload.get("end_user"),
        user_agent=payload.get("user_agent") or meta.get("user_agent"),
        client_headers=meta.get("requester_custom_headers") or None,
        params=payload.get("model_parameters") or {},
        messages=payload.get("messages"),
    )


def day_file(log_dir: pathlib.Path, day: dt.date) -> pathlib.Path | None:
    """The day's file, plain while the day is open or just closed, gzipped after rotation; None if neither."""
    plain = log_dir / day_file_name(day)
    if plain.exists():
        return plain
    packed = plain.with_name(plain.name + ".gz")
    return packed if packed.exists() else None


def read_day(log_dir: pathlib.Path, day: dt.date) -> Iterator[edict]:
    """Every record of one day, plain or compressed; nothing when there is no file.

    A line that is not JSON is skipped rather than stopping the day: a
    write cut short by a restart leaves one, and one bad line must not
    cost the other hundred thousand.
    """
    path = day_file(log_dir, day)
    if path is None:
        return
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                yield edict(json.loads(line))
            except ValueError:
                continue


# ---- what counts as demand ----------------------------------------------
#
# Shared with the fold of LiteLLM's spend log in llmao/capacity.py, so the
# two sides of a demand row agree on what a request to a model is. A row
# with no model group (a bad key refused at the door, a /v1/models listing)
# is not use of a model, and LiteLLM's own health checks, which run as
# their own team, are not anyone's demand. Nor is a request that names a
# deployment by its LiteLLM id instead of a model: the fleet tools and
# health probes do that, one eight-token completion per deployment.

HEALTH_CHECK_TEAM = "litellm-internal-health-check"
DEPLOYMENT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def counts_as_demand(model: str | None, project: str | None) -> bool:
    """Whether a request to `model` from `project` is someone's use of a model."""
    return bool(model) and not DEPLOYMENT_ID.match(model) and project != HEALTH_CHECK_TEAM


# Prompt length in tokens, bucketed: is anyone using the context we pay KV
# cache for. The last bucket has no upper bound.
PROMPT_BUCKETS = ((1_000, "<1k"), (8_000, "1k-8k"), (32_000, "8k-32k"), (128_000, "32k-128k"), (None, ">128k"))
NO_PROJECT = "(no project)"


def prompt_bucket(tokens: int) -> str:
    for limit, name in PROMPT_BUCKETS:
        if limit is None or tokens < limit:
            return name
    return PROMPT_BUCKETS[-1][1]


def tool_family(user_agent: str | None) -> str:
    """The tool behind a user agent: the product before the first slash.

    "opencode/latest/2.2.1/acp" is opencode, "OpenAI/Python 1.109.1" is the
    OpenAI SDK, "codex-tui/0.166.0 (...)" is codex-tui. Every browser says
    Mozilla first, so those fold into one; a request with no user agent is
    "unknown". This is the same cut LiteLLM makes for its User-Agent tags.
    """
    name = (user_agent or "").split("/", 1)[0].strip()
    if not name:
        return "unknown"
    return "browser" if name == "Mozilla" else name


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile: the smallest value that p of them are at or below."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p * len(ordered)) - 1)]


# ---- the rollup of a day ------------------------------------------------
#
# What only the log can say, per model per hour, folded into the demand
# rows beside what LiteLLM's spend log said (docs/prompt-logging-design.md,
# "Demand rows"): the stream flag, tools in the request, the finish
# reason, the time to first token. And the day's request count per model
# per hour, the log side of the reconciliation with LiteLLM's count.


def rollup(records: Iterable[Mapping]) -> edict:
    """A day's records as {records, hours}: per model per hour, what only the log can say, and the request count."""
    by_hour: dict[tuple[str, str], list] = defaultdict(list)
    seen = 0
    for r in records:
        seen += 1
        key = r.get("key") or {}
        if not counts_as_demand(r.get("model"), key.get("project")) or not r.get("ts"):
            continue
        by_hour[(r["ts"][:13], r["model"])].append(r)
    hours = []
    for (hour, model), mine in sorted(by_hour.items()):
        ttft = [r["ttft_s"] for r in mine if r.get("ttft_s") is not None]
        hours.append(
            edict(
                hour=hour,
                model=model,
                requests=len(mine),
                streamed=sum(1 for r in mine if r.get("stream")),
                with_tools=sum(1 for r in mine if (r.get("params") or {}).get("tools")),
                finish_length=sum(1 for r in mine if r.get("finish_reason") == "length"),
                ttft_p50_s=percentile(ttft, 0.5),
                ttft_p95_s=percentile(ttft, 0.95),
            )
        )
    return edict(records=seen, hours=hours)


def rollup_json(log_dir: pathlib.Path, day: dt.date) -> edict:
    """What `llmao-prompt-log rollup --json <day>` prints: the rollup, and whether the day had a file."""
    found = day_file(log_dir, day) is not None
    out = rollup(read_day(log_dir, day)) if found else edict(records=0, hours=[])
    return edict(day=day.isoformat(), found=found, **out)


class RollupError(Exception):
    """The tool did not produce a day's rollup: it failed, or took too long."""


# The tool, beside the module's package; run by the portal's own interpreter.
TOOL = pathlib.Path(__file__).resolve().parent.parent / "bin" / "llmao-prompt-log"


async def rollup_day(
    day: dt.date, log_dir: pathlib.Path, *, timeout_s: float = 600, tool: pathlib.Path = TOOL
) -> edict:
    """A day's rollup from the tool, run as a child: the parsing happens on another core.

    The child runs under nice and ionice where they exist, since it shares
    the host with the proxy, and is killed at the timeout. A failure is
    RollupError, with the tool's last words; the caller decides what a
    day without its log side means. llmao never opens a log file itself.
    """
    argv = [sys.executable, str(tool), "rollup", "--json", day.isoformat(), "--log-dir", str(log_dir)]
    if shutil.which("ionice"):
        argv = ["ionice", "-c", "3", *argv]
    if shutil.which("nice"):
        argv = ["nice", "-n", "10", *argv]
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RollupError(f"{tool.name} took longer than {timeout_s:g}s on {day}") from None
    if proc.returncode != 0:
        last = err.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"]
        raise RollupError(f"{tool.name} exited {proc.returncode} on {day}: {last[0]}")
    try:
        return edict(json.loads(out))
    except ValueError as e:
        raise RollupError(f"{tool.name} printed no JSON for {day}") from e


def log_dir(cfg: Any) -> pathlib.Path:
    """config.yaml prompt_log_dir: where the proxy's hook writes the day files. Call at startup.

    Required, like every other setting with an operational effect: the
    rollup reads there, and a missing value is a startup error rather
    than a quiet day without its log side. Puppet renders the same path
    into litellm.yaml for the hook.
    """
    raw = cfg.get("prompt_log_dir")
    if not raw:
        raise ValueError("config.yaml: prompt_log_dir is required")
    return pathlib.Path(str(raw))


# ---- the reports --------------------------------------------------------
#
# For an admin at a shell on the host: the first analyses, chosen because
# they need no reading of prompt content. The module makes the figures;
# the tool prints them as text or CSV.


def _tally(bucket: dict, name: str, tokens: int) -> None:
    acc = bucket.setdefault(name, edict(requests=0, prompt_tokens=0))
    acc.requests += 1
    acc.prompt_tokens += tokens


def _ranked(bucket: dict) -> list[edict]:
    return [edict(name=k, **v) for k, v in sorted(bucket.items(), key=lambda kv: -kv[1].requests)]


def report(records: Iterable[Mapping]) -> edict:
    """The figures of the design's first reports, over whatever records are given.

    ``by_project``, ``by_user``, ``by_model``, ``by_agent``: requests and
    prompt tokens, most requests first. ``prompt_len``: per model, counts
    in the length buckets. ``shares``: per model, how many requests
    streamed, carried tools, or ended at max_tokens. ``failures``: per
    model and error class. ``conversation``: per model, messages per
    request at the median and the 95th percentile.
    """
    by_project: dict = {}
    by_user: dict = {}
    by_model: dict = {}
    by_agent: dict = {}
    prompt_len: dict[str, dict] = defaultdict(lambda: dict.fromkeys([b for _l, b in PROMPT_BUCKETS], 0))
    shares: dict[str, edict] = defaultdict(lambda: edict(requests=0, streamed=0, with_tools=0, finish_length=0))
    failures: dict[tuple[str, str], int] = defaultdict(int)
    messages: dict[str, list[int]] = defaultdict(list)
    days: set[str] = set()
    seen = 0
    for r in records:
        key = r.get("key") or {}
        model = r.get("model")
        if not counts_as_demand(model, key.get("project")):
            continue
        seen += 1
        if r.get("ts"):
            days.add(r["ts"][:10])
        tokens = (r.get("tokens") or {}).get("prompt") or 0
        _tally(by_project, key.get("project") or NO_PROJECT, tokens)
        _tally(by_user, key.get("user") or "(automation)", tokens)
        _tally(by_model, model, tokens)
        _tally(by_agent, tool_family(r.get("user_agent")), tokens)
        share = shares[model]
        share.requests += 1
        share.streamed += bool(r.get("stream"))
        share.with_tools += bool((r.get("params") or {}).get("tools"))
        share.finish_length += r.get("finish_reason") == "length"
        if r.get("status") == "failure":
            failures[(model, ((r.get("error") or {}).get("class")) or "unknown")] += 1
            continue
        prompt_len[model][prompt_bucket(tokens)] += 1
        if isinstance(r.get("messages"), list):
            messages[model].append(len(r["messages"]))
    return edict(
        records=seen,
        days=sorted(days),
        by_project=_ranked(by_project),
        by_user=_ranked(by_user),
        by_model=_ranked(by_model),
        by_agent=_ranked(by_agent),
        prompt_len=[edict(model=m, **prompt_len[m]) for m in sorted(prompt_len)],
        shares=[edict(model=m, **shares[m]) for m in sorted(shares)],
        failures=[
            edict(model=m, error=e, count=n)
            for (m, e), n in sorted(failures.items(), key=lambda kv: (kv[0][0], -kv[1]))
        ],
        conversation=[
            edict(model=m, p50=percentile(messages[m], 0.5), p95=percentile(messages[m], 0.95))
            for m in sorted(messages)
        ],
    )


def read_days(log_dir: pathlib.Path, days: Iterable[dt.date]) -> Iterator[edict]:
    """Every record of several days, in day order."""
    for day in days:
        yield from read_day(log_dir, day)


# ---- the accessor for scripts -------------------------------------------
#
# What a script at a shell on the host needs to get at the records, and
# nothing of what it then does with them: the content analyses are R&D in
# bin/ (docs/prompt-logging-design.md, "The post-process") and stay out of
# the library until they settle.

CONFIG = pathlib.Path("config.yaml")
DEFAULT_LOG_DIR = pathlib.Path("prompt-log")


def configured_log_dir(config: pathlib.Path = CONFIG) -> pathlib.Path:
    """prompt_log_dir from config.yaml in the current directory, else ./prompt-log.

    The lenient cousin of ``log_dir``: a script run from a checkout without a
    config.yaml gets the development default, where the portal gets an error.
    """
    if config.exists():
        import yaml

        raw = (yaml.safe_load(config.read_text()) or {}).get("prompt_log_dir")
        if raw:
            return pathlib.Path(str(raw))
    return DEFAULT_LOG_DIR


def span(*, days: int = 7, start: dt.date | None = None, end: dt.date | None = None) -> list[dt.date]:
    """The UTC days to read: the last ``days`` closed ones, or ``start`` to ``end``, in order.

    ``end`` defaults to yesterday, the last closed day; ``start`` to ``days``
    before it. A start after its end is a ValueError.
    """
    end = end or dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1)
    start = start or end - dt.timedelta(days=days - 1)
    if start > end:
        raise ValueError(f"span starts {start}, after it ends {end}")
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]

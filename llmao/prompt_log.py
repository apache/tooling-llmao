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

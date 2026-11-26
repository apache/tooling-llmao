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
litellm_hooks.py and read by bin/llmao-prompt-log for the demand rollup
and by the scripts in analysis/. Design: docs/prompt-logging-design.md,
"The record" and "Files".

This module is imported by the hook inside the proxy process, by the
tool, by the scripts, and by the rollup in llmao/capacity.py, so it
imports nothing heavy: no loguru (the hook owns the sink), nothing of
LiteLLM, and it never reads config.yaml (the portal has APP.cfg, the
scripts have analysis/_core.py). The
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
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from box import Box

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


def _error(info: Mapping | None) -> dict | None:
    """The error's class, code and provider; never the message, which can quote the prompt."""
    if not info:
        return None
    return {
        "class": info.get("error_class"),
        "code": info.get("error_code"),
        "provider": info.get("llm_provider"),
        "normalized": info.get("normalized_error"),
    }


# Header names whose VALUE is a credential. A pattern rather than a list,
# because the list is the part that goes stale: MCP federation forwards
# upstream credentials as x-mcp-<alias>-authorization and friends, and those
# are custom headers by definition, so they are exactly what lands in
# requester_custom_headers. A name-shaped denylist would have missed every
# one of them.
_SECRET_HEADER = re.compile(r"auth|token|secret|credential|cookie|api[-_]?key|bearer|password", re.I)


def safe_headers(raw: Any) -> dict | None:
    """Client headers with the credential-bearing values masked.

    Names are kept: knowing a caller sent an Authorization header is useful
    and the name is not the secret. Only the value goes.
    """
    # dict, not Mapping: Mapping is imported for annotations only here, and
    # the field arrives JSON-derived from LiteLLM's metadata anyway.
    if not isinstance(raw, dict):
        return None
    masked = {str(k): ("***" if _SECRET_HEADER.search(str(k)) else v) for k, v in raw.items()}
    return masked or None


def key_purpose(kwargs: Mapping) -> str | None:
    """The key's purpose, the name typed when the key was made, from the callback's kwargs.

    The portal stores it as ``purpose`` in the key's metadata (never as
    LiteLLM's globally unique ``key_alias``), and the proxy copies the
    key's metadata into the request's before the call. LiteLLM's standard
    payload does not carry it, so it is read from the request's params.
    """
    metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
    return (metadata.get("user_api_key_metadata") or {}).get("purpose")


def record(payload: Mapping, purpose: str | None = None) -> dict:
    """One record from LiteLLM's StandardLoggingPayload, the fields in the design's table.

    The prompt is kept (``messages`` as LiteLLM received it, and the
    request's parameters); the response is not, apart from its finish
    reason. Field names are ours. ``purpose`` is the key's, from
    key_purpose(); it is the only field not in the payload.

    A record is a plain dict, not a Box, here and when read back:
    ``params`` and ``messages`` are trees the client wrote, with keys we
    do not choose, so readers use get(), never an attribute. (A tool
    schema's "items" key once broke json.dumps on the EasyDict this was;
    a Box keeps the method and would not break, but the boundary stays.)
    """
    meta = payload.get("metadata") or {}
    streamed = bool(payload.get("stream"))
    start, end = payload.get("startTime"), payload.get("endTime")
    return dict(
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
        tokens=dict(
            prompt=payload.get("prompt_tokens") or 0,
            completion=payload.get("completion_tokens") or 0,
            total=payload.get("total_tokens") or 0,
        ),
        finish_reason=_finish_reason(payload.get("response")),
        key=dict(
            hash=meta.get("user_api_key_hash"),
            purpose=purpose,
            user=meta.get("user_api_key_user_id"),
            project=meta.get("user_api_key_team_alias"),
        ),
        end_user=payload.get("end_user"),
        user_agent=payload.get("user_agent") or meta.get("user_agent"),
        client_headers=safe_headers(meta.get("requester_custom_headers")),
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


def read_day(log_dir: pathlib.Path, day: dt.date) -> Iterator[dict]:
    """Every record of one day, plain or compressed; nothing when there is no file.

    A line that is not JSON is skipped rather than stopping the day: a
    write cut short by a restart leaves one, and one bad line must not
    cost the other hundred thousand. Plain dicts, as record() says.
    """
    path = day_file(log_dir, day)
    if path is None:
        return
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
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
# A label llmao made up, never an id from the data: the "llmao:" prefix says
# so wherever one is tallied or printed, and a colon cannot appear in an ASF
# project or user id. One word, so a row of output splits on whitespace.
NO_PROJECT = "llmao:none"


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


def rollup(records: Iterable[Mapping]) -> Box:
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
            Box(
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
    return Box(records=seen, hours=hours)


def rollup_json(log_dir: pathlib.Path, day: dt.date) -> Box:
    """What `llmao-prompt-log rollup --json <day>` prints: the rollup, and whether the day had a file."""
    found = day_file(log_dir, day) is not None
    out = rollup(read_day(log_dir, day)) if found else Box(records=0, hours=[])
    return Box(day=day.isoformat(), found=found, **out)


class RollupError(Exception):
    """The tool did not produce a day's rollup: it failed, or took too long."""


# The tool, beside the module's package; run by the portal's own interpreter.
TOOL = pathlib.Path(__file__).resolve().parent.parent / "bin" / "llmao-prompt-log"


async def rollup_day(day: dt.date, log_dir: pathlib.Path, *, timeout_s: float = 600, tool: pathlib.Path = TOOL) -> Box:
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
        return Box(json.loads(out))
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
# analysis/prompts report prints them as text or CSV.
#
# The figures of one day are one JSON document, kept in the analysis store
# (figures_json on its days table) when build reads the day, so a report
# over a span adds documents up instead of reading the day files again.
# They add because every figure is a count: Counters, which add with
# update() and rank with most_common().

DIMENSIONS = ("project", "user", "model", "agent")  # what requests and prompt tokens are counted by


@dataclass
class Figures:
    """One day's figures, or several days' added up.

    Every mapping holds Counters. A dataclass rather than a Box, the one
    place this module makes that choice: a Box turns any dict it holds
    into a Box, a Counter among them, and with it go update() and
    most_common(). ``requests`` and ``prompt_tokens`` are keyed by
    dimension (DIMENSIONS), then name; the rest by model. The messages
    histogram is keyed by the count as a string, which is what JSON hands
    back.
    """

    records: int = 0
    days: list[str] = field(default_factory=list)  # the UTC days the records carry, "YYYY-MM-DD"
    requests: dict[str, Counter] = field(default_factory=dict)
    prompt_tokens: dict[str, Counter] = field(default_factory=dict)
    prompt_len: dict[str, Counter] = field(default_factory=dict)  # length bucket -> successful requests
    shares: dict[str, Counter] = field(default_factory=dict)  # requests, streamed, with_tools, finish_length
    failures: dict[str, Counter] = field(default_factory=dict)  # error class -> count
    messages: dict[str, Counter] = field(default_factory=dict)  # messages per request -> successful requests

    COUNTED = ("requests", "prompt_tokens", "prompt_len", "shares", "failures", "messages")  # what merge adds

    @classmethod
    def from_records(cls, records: Iterable[Mapping]) -> Figures:
        fig = cls()
        for r in records:
            fig.add(r)
        return fig

    def add(self, r: Mapping) -> None:
        """Count one record in; a record that is not demand is not counted."""
        key = r.get("key") or {}
        model = r.get("model")
        if not counts_as_demand(model, key.get("project")):
            return
        self.records += 1
        if r.get("ts") and r["ts"][:10] not in self.days:
            self.days.append(r["ts"][:10])
        tokens = (r.get("tokens") or {}).get("prompt") or 0
        names = Box(
            project=key.get("project") or NO_PROJECT,
            user=key.get("user") or "(automation)",
            model=model,
            agent=tool_family(r.get("user_agent")),
        )
        for dim, name in names.items():
            self.requests.setdefault(dim, Counter())[name] += 1
            self.prompt_tokens.setdefault(dim, Counter())[name] += tokens
        share = self.shares.setdefault(model, Counter())
        share["requests"] += 1
        share["streamed"] += bool(r.get("stream"))
        share["with_tools"] += bool((r.get("params") or {}).get("tools"))
        share["finish_length"] += r.get("finish_reason") == "length"
        if r.get("status") == "failure":
            self.failures.setdefault(model, Counter())[((r.get("error") or {}).get("class")) or "unknown"] += 1
            return
        self.prompt_len.setdefault(model, Counter())[prompt_bucket(tokens)] += 1
        if isinstance(r.get("messages"), list):
            self.messages.setdefault(model, Counter())[str(len(r["messages"]))] += 1

    def merge(self, part: Figures) -> Figures:
        """Add another day's figures in, in place; returns self. The part may be as from_json read it."""
        self.records += part.records
        self.days = sorted(set(self.days) | set(part.days))
        for name in self.COUNTED:
            mine = getattr(self, name)
            for k, counts in getattr(part, name).items():
                mine.setdefault(k, Counter()).update(counts)
        return self

    def tables(self) -> Box:
        """The figures as the tables ``prompts report`` prints: ranked, bucketed, percentiles taken.

        ``by_project``, ``by_user``, ``by_model``, ``by_agent``: requests
        and prompt tokens, most requests first. ``prompt_len``: per model,
        counts in the length buckets. ``shares``: per model, how many
        requests streamed, carried tools, or ended at max_tokens.
        ``failures``: per model and error class. ``conversation``: per
        model, messages per request at the median and the 95th percentile.
        """

        def ranked(dim: str) -> list[Box]:
            tokens = self.prompt_tokens.get(dim, Counter())
            requests = self.requests.get(dim, Counter())
            return [Box(name=n, requests=c, prompt_tokens=tokens[n]) for n, c in requests.most_common()]

        def lengths(model: str) -> list[int]:
            return [int(n) for n in self.messages[model].elements()]

        buckets = [b for _l, b in PROMPT_BUCKETS]
        return Box(
            records=self.records,
            days=sorted(self.days),
            **{f"by_{dim}": ranked(dim) for dim in DIMENSIONS},
            prompt_len=[Box(model=m, **{b: self.prompt_len[m][b] for b in buckets}) for m in sorted(self.prompt_len)],
            shares=[Box(model=m, **self.shares[m]) for m in sorted(self.shares)],
            failures=[
                Box(model=m, error=e, count=n) for m in sorted(self.failures) for e, n in self.failures[m].most_common()
            ],
            conversation=[
                Box(model=m, p50=percentile(lengths(m), 0.5), p95=percentile(lengths(m), 0.95))
                for m in sorted(self.messages)
            ],
        )

    def to_json(self) -> str:
        return json.dumps(vars(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Figures:
        """A day as the store kept it; its mappings are plain, which update() takes as it takes a Counter."""
        return cls(**json.loads(text))

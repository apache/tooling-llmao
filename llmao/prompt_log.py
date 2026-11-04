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

import datetime as dt
import gzip
import json
from typing import TYPE_CHECKING, Any

from easydict import EasyDict as edict  # noqa: N813

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterator, Mapping

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

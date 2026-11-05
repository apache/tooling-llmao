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

"""The prompt log's reports, for a person at a shell on the gateway host.

    prompts report                 # the last 7 closed days
    prompts report --days 30
    prompts report --from 2026-11-01 --to 2026-11-03 --csv

Reads the day files the proxy's hook writes (docs/prompt-logging-design.md)
and nothing else: never a connection to LiteLLM, never a write in the log
directory. None of the reports reads prompt content: requests and prompt
tokens by project, user, model and tool; prompt length per model; the
share of requests that stream, carry tools or end at max_tokens; failures
by class; and messages per request.

The log directory is --log-dir, else prompt_log_dir from config.yaml in
the current directory; without either the script stops.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import _core

from llmao import fmt, prompt_log

if TYPE_CHECKING:
    import argparse
    import datetime as dt
    import pathlib


def report(log_dir: pathlib.Path, days: list[dt.date], *, csv_out: bool) -> None:
    rep = prompt_log.report(prompt_log.read_days(log_dir, days))
    span = f"{days[0]} to {days[-1]}" if len(days) > 1 else str(days[0])
    if not csv_out:
        print(f"{fmt.plural(rep.records, 'request')} over {span}, {fmt.plural(len(rep.days), 'day')} with records\n")
    n = max(1, len(days))
    for title, rows in (
        ("By project", rep.by_project),
        ("By user", rep.by_user),
        ("By model", rep.by_model),
        ("By tool", rep.by_agent),
    ):
        _core.table(
            title,
            ["name", "requests", "per day", "prompt tokens"],
            [[r.name, r.requests, round(r.requests / n, 1), r.prompt_tokens] for r in rows],
            csv_out=csv_out,
        )
    buckets = [b for _l, b in prompt_log.PROMPT_BUCKETS]
    _core.table(
        "Prompt length, successful requests",
        ["model", *buckets],
        [[r.model, *(r[b] for b in buckets)] for r in rep.prompt_len],
        csv_out=csv_out,
    )
    _core.table(
        "Shares, all requests",
        ["model", "requests", "streamed", "with tools", "cut off at max_tokens"],
        [[r.model, r.requests, r.streamed, r.with_tools, r.finish_length] for r in rep.shares],
        csv_out=csv_out,
    )
    _core.table(
        "Failures by class",
        ["model", "error", "count"],
        [[r.model, r.error, r.count] for r in rep.failures],
        csv_out=csv_out,
    )
    _core.table(
        "Messages per request",
        ["model", "median", "95th percentile"],
        [[r.model, r.p50, r.p95] for r in rep.conversation],
        csv_out=csv_out,
    )


def run(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    try:
        days = _core.span(days=args.days, start=args.start, end=args.end)
    except ValueError:
        ap.error("--from is after --to")
    report(args.log_dir or _core.log_dir(), days, csv_out=args.csv)
    return 0

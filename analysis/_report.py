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

Reads the store, never the day files: build keeps each day's figures as it
reads the day (docs/prompt-logging-design.md for the files), and a report
over a span adds the days up, so it returns at once however long the span.
A day the store has not read is not in the report; a day read before the
figures were kept stops it, saying to rebuild. None of the reports reads
prompt content: requests and prompt tokens by project, user, model and
tool; prompt length per model; the share of requests that stream, carry
tools or end at max_tokens; failures by class; and messages per request.

The store is --analysis-dir, else analysis_dir from config.yaml.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import _core

from llmao import fmt, prompt_log

if TYPE_CHECKING:
    import argparse
    import datetime as dt


def report(store: _core.Store, days: list[dt.date], *, csv_out: bool) -> None:
    kept = store.figures_over(days)
    unkept = [day for day, fig in kept if fig is None]
    if unkept:
        raise SystemExit(
            f"{fmt.plural(len(unkept), 'day')} of the span ({unkept[0]} first) were read before the report's "
            "figures were kept with them: run prompts build --rebuild, then report again"
        )
    total = prompt_log.Figures()
    for _day, fig in kept:
        _ = total.merge(fig)
    rep = total.tables()
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
    store = _core.Store(args.analysis_dir or _core.analysis_dir())
    try:
        report(store, days, csv_out=args.csv)
    finally:
        store.close()
    return 0

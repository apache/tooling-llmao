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

"""The judges' verdict on each cluster: Foundation work or not, the tier that would do, the activity.

    sudo ./analysis/prompt-judge                  # dry run: the first request it would send, the counts
    sudo ./analysis/prompt-judge --judge          # send them, one at a time, judge after judge, then list
    sudo ./analysis/prompt-judge --judge --days 30 --limit 20   # at most 20 calls per judge this run
    sudo ./analysis/prompt-judge --judge --model qwen3-8b       # one judge, instead of the configured list
    sudo ./analysis/prompt-judge --findings       # the listing only
    sudo ./analysis/prompt-judge --findings --compare   # each judge's answers, where they differ
    sudo ./analysis/prompt-judge --findings --reasons   # each judge's reason, cluster by cluster
    sudo ./analysis/prompt-judge --findings --json

Several judges on each cluster is the normal operation: config.yaml
analysis_models lists them, and --model names one or more instead. Reads
the clusters prompt-clusters stored for the span. Without --judge it is
a dry run: it prints the request it would send first and how many
clusters each judge still has to answer, and sends nothing. With --judge
it sends the gateway one request per cluster per judge that has no
verdict yet from this judge prompt (analysis/judge-prompt.md), this
model and this exemplar. Each verdict is written as it returns, so a run
that stops keeps what it judged. One request in flight, through the
front door, with the service key in config.yaml analysis_key. Clusters
from the master key are the operators' smoke tests and are never judged.

The listing is every cluster of the span with what its judges together
say (the store's consensus view: Foundation work leans towards "no",
the tier towards "high", the activity is the majority), a mark where
they differ, then the tier report: the judged tier against the models
the cluster used, and how many requests went to a heavier model than
the judges think the work needs (config.yaml analysis_tiers says which
model serves which tier). A cluster the judges called non-Foundation
work, or one at the top of the tier report, is what an admin reads it
for.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
from typing import TYPE_CHECKING

import _core

from llmao import fmt

if TYPE_CHECKING:
    import argparse

OPENING_SHOWN = 48


def _table(title: str, columns: list[str], rows: list[list[str]], *, numeric: set[int]) -> None:
    print(title)
    widths = [max(len(col), *(len(r[i]) for r in rows)) if rows else len(col) for i, col in enumerate(columns)]

    def line(values):
        return "  ".join(
            v.rjust(w) if i in numeric else v.ljust(w) for i, (v, w) in enumerate(zip(values, widths, strict=True))
        )

    print(line(columns))
    print(line(["-" * w for w in widths]))
    for r in rows:
        print(line(r))
    print()


def _models(c) -> str:
    return ", ".join(f"{m} {n}" for m, n in sorted(c.models.items(), key=lambda kv: -kv[1]))


def _opening(c) -> str:
    return c.opening[:OPENING_SHOWN] + ("…" if len(c.opening) > OPENING_SHOWN else "")


def _said(v) -> str:
    return f"{v.asf_work} {v.tier} {v.activity}" if v else "-"


def _findings(clusters: list, days: list[dt.date], by_tier: dict, *, compare: bool, reasons: bool) -> None:
    span = f"{days[0]} to {days[-1]}" if len(days) > 1 else str(days[0])
    smoke = [c for c in clusters if c.who == _core.MASTER_KEY]
    judged = [c for c in clusters if c.verdict and c.who != _core.MASTER_KEY]
    judges = sorted({v.model for c in judged for v in c.verdicts})
    print(
        f"{fmt.plural(len(clusters), 'cluster')} over {span}, {len(judged)} with a verdict; "
        f"judges: {', '.join(judges) or 'none'}\n"
    )

    rows = []
    split = False
    for c in clusters:
        v = c.verdict if c.who != _core.MASTER_KEY else None
        if c.who == _core.MASTER_KEY:
            answers = ["-", "-", "(smoke) [1]"]
        elif v:
            # A mark where the judges did not all say the same.
            answers = [
                v.asf_work + ("*" if v.asf_answers > 1 else ""),
                v.tier + ("*" if v.tier_answers > 1 else ""),
                v.activity + ("*" if v.activity_answers > 1 else ""),
            ]
            split = split or v.asf_answers > 1 or v.tier_answers > 1 or v.activity_answers > 1
        else:
            answers = ["-", "-", "-" if c.exemplar_hash else "(no exemplar)"]
        judges_n = str(v.judges) if v else ""
        rows.append([c.fingerprint, c.project, c.who, c.tool, f"{c.requests:,}", judges_n, *answers, _models(c)])
        rows[-1].append(_opening(c))
    _table(
        "Verdicts: the judges' consensus",
        ["fingerprint", "project", "who", "tool", "requests", "judges", "asf", "tier", "activity", "models", "opening"],
        rows,
        numeric={4, 5},
    )
    if split:
        print("* the judges did not all say the same; --compare shows each one's answer.\n")

    report = [r for r in _core.tier_report(judged, by_tier) if r.above or r.below or r.unranked]
    if report:
        _table(
            "Tier: what was used against what the judges say would do",
            [
                "fingerprint",
                "project",
                "who",
                "requests",
                "consensus",
                "recommended",
                "used",
                "heavier",
                "lighter",
                "unranked",
            ],
            [
                [
                    r.cluster.fingerprint,
                    r.cluster.project,
                    r.cluster.who,
                    f"{r.cluster.requests:,}",
                    r.tier,
                    r.recommended,
                    _models(r.cluster),
                    f"{r.above:,}" if r.above else "",
                    f"{r.below:,}" if r.below else "",
                    f"{r.unranked:,}" if r.unranked else "",
                ]
                for r in report
            ],
            numeric={3, 7, 8, 9},
        )
        print(
            "heavier: requests on a model above the judged tier, which a cheaper model would have served; "
            "lighter: below it; unranked: a model the tier map does not place (config.yaml analysis_tiers).\n"
        )

    if compare and judges:
        differ = [c for c in judged if len({_said(v) for v in c.verdicts}) > 1]
        _table(
            f"Where the judges differ ({len(differ)} of {len(judged)} judged)",
            ["fingerprint", "project", "who", "requests", *judges],
            [
                [c.fingerprint, c.project, c.who, f"{c.requests:,}"]
                + [_said(next((v for v in c.verdicts if v.model == m), None)) for m in judges]
                for c in differ
            ],
            numeric={3},
        )

    if reasons:
        for c in judged:
            print(c.fingerprint)
            for v in c.verdicts:
                print(f"  {v.model}: {v.reason}")
        print()
    if smoke:
        print(f"[1] {fmt.plural(len(smoke), 'cluster')} from the master key: the operators' smoke tests, not judged.")


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--days", type=int, default=7, help="the last N closed UTC days (default %(default)s)")
    ap.add_argument("--from", dest="start", type=dt.date.fromisoformat, help="first day, instead of --days")
    ap.add_argument("--to", dest="end", type=dt.date.fromisoformat, help="last day (default: yesterday)")
    ap.add_argument("--analysis-dir", type=pathlib.Path, default=None, help="where the store lives")
    ap.add_argument("--judge", action="store_true", help="send the requests; without it nothing is sent")
    ap.add_argument("--limit", type=int, default=None, help="at most this many calls per judge this run")
    ap.add_argument("--model", action="append", default=None, help="a judge, instead of analysis_models; repeatable")
    ap.add_argument("--compare", action="store_true", help="in the listing, each judge's answers where they differ")
    ap.add_argument("--reasons", action="store_true", help="in the listing, each judge's reason")
    ap.add_argument("--findings", action="store_true", help="the listing only")
    ap.add_argument("--json", action="store_true", help="the clusters with their verdicts as JSON")


def run(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    try:
        days = _core.span(days=args.days, start=args.start, end=args.end)
    except ValueError:
        ap.error("--from is after --to")

    prompt = _core.judge_prompt()
    settings = _core.judge_settings() if args.judge else None
    judges = args.model or (settings.models if settings else _core.judge_models())
    by_tier = _core.tiers()
    _core.require_models({*judges, *(m for ms in by_tier.values() for m in ms)})
    _core.require_private(judges)
    store = _core.Store(args.analysis_dir or _core.analysis_dir())
    try:
        clusters = list(store.clusters_over(days))
        if not args.findings:
            todo = {
                model: [c for c in store.stale(clusters, prompt.hash, model) if c.who != _core.MASTER_KEY][: args.limit]
                for model in judges
            }
            if not args.judge:
                first = next((cs[0] for cs in todo.values() if cs), None)
                if first:
                    print(json.dumps(_core.judge_request(first, prompt, judges[0]), indent=1))
                counts = ", ".join(f"{m} {len(cs)}" for m, cs in todo.items())
                print(f"\nclusters to judge: {counts}; nothing sent without --judge", file=sys.stderr)
                return 0
            for model, cs in todo.items():
                for i, c in enumerate(cs, 1):
                    try:
                        verdict = _core.parse_verdict(_core.ask(settings, _core.judge_request(c, prompt, model)))
                    except _core.JudgeError as e:
                        raise SystemExit(f"{model} on {c.fingerprint}: {e}") from None
                    store.write_verdict(c, prompt.hash, model, verdict)
                    print(f"{model} {i}/{len(cs)} {c.fingerprint} {_said(verdict)}", file=sys.stderr)
        clusters = list(store.with_consensus(clusters, prompt.hash))
        if args.json:
            json.dump(clusters, sys.stdout, indent=1)
            sys.stdout.write("\n")
        else:
            _findings(clusters, days, by_tier, compare=args.compare, reasons=args.reasons)
    finally:
        store.close()
    return 0

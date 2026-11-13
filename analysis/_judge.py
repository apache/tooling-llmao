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

"""The judges: their verdict on each cluster, and the reports read from the verdicts.

    prompts judge                       # dry run: the clusters to judge, the calls --judge would make, where they go
    prompts judge --judge               # send them, one at a time, judge after judge
    prompts judge --judge --limit 20    # at most 20 calls per judge this run
    prompts judge --judge --model qwen3-8b   # one judge, instead of the configured list
    prompts verdicts                    # the judges' consensus per cluster, marked where they split
    prompts verdicts --compare --reasons     # each judge's answers where they differ, and each one's reason
    prompts tiers                       # what was used against what the judges say would do

Several judges on each cluster is the normal operation: config.yaml
analysis_models lists them, and --model names one or more instead. Each
is asked the three questions of analysis/judge-prompt.md about every
cluster of the span that has no verdict yet from that judge prompt,
that model and the cluster's present exemplar; each verdict is written
as it returns, so a run that stops keeps what it judged. One request in
flight, through the front door, with the service key in config.yaml
analysis_key. Clusters from the master key are the operators' smoke
tests and are never judged. A judge must run only on our rentals.

verdicts reads the store's consensus view: Foundation work leans
towards "no", the tier towards "high", the activity is the majority. A
cluster the judges called non-Foundation work is what an admin reads it
for. tiers sets the consensus tier against the models the cluster used,
with config.yaml analysis_tiers saying which model serves which tier:
the requests on a heavier model than the work needs are the saving to
suggest, those on a lighter one the risk.
"""

from __future__ import annotations

import collections
import json
import sys
from typing import TYPE_CHECKING

import _core

from llmao import fmt

if TYPE_CHECKING:
    import argparse
    import datetime as dt

OPENING_SHOWN = 48


def _said(v) -> str:
    return f"{v.asf_work} {v.tier} {v.activity}" if v else "-"


def _open(args: argparse.Namespace) -> _core.Store:
    return _core.Store(args.analysis_dir or _core.analysis_dir())


def _consensus(args: argparse.Namespace, days: list[dt.date], prompt_hash: str) -> list:
    store = _open(args)
    try:
        return list(store.with_consensus(store.clusters_over(days), prompt_hash))
    finally:
        store.close()


# ---- judge ----------------------------------------------------------------

SECONDS_PER_CALL = 2.5  # measured on the int4 27b; the docs quote it


def _dry_run(todo: dict[str, list], judges: list[str], days: list[dt.date], prompt, *, request: bool) -> None:
    """What --judge would do: the clusters, which judges each lacks, the calls, the time, and where they go."""
    lacking: dict[str, list[str]] = {}
    for model, cs in todo.items():
        for c in cs:
            lacking.setdefault(c.fingerprint, []).append(model)
    by_fp = {c.fingerprint: c for cs in todo.values() for c in cs}
    calls = sum(len(cs) for cs in todo.values())
    print(f"dry run over {_core.span_text(days)}: {fmt.plural(len(by_fp), 'cluster')} lack a verdict from some judge\n")
    if not by_fp:
        print("nothing to do: every cluster has a verdict from every judge")
        return
    rows = [
        [
            c.fingerprint,
            c.project,
            c.who,
            c.tool,
            c.requests,
            "every judge" if len(ms) == len(judges) else ", ".join(ms),
        ]
        for fp, ms in lacking.items()
        for c in (by_fp[fp],)
    ]
    _core.table("To judge", ["fingerprint", "project", "who", "tool", "requests", "lacks"], rows)
    per_judge = ", ".join(f"{len(cs)} to {m}" for m, cs in todo.items() if cs)
    seconds = calls * SECONDS_PER_CALL
    about = f"{seconds / 60:.0f} minutes" if seconds >= 90 else f"{seconds:.0f} seconds"
    print(f"\nwith --judge: {fmt.plural(calls, 'call')}, about {about} at {SECONDS_PER_CALL:g} s a call")
    print(f"  {per_judge}")
    print(f"  each through {_core.gateway()} with the {_core.ANALYSIS_KEY_PURPOSE} key")
    print("a verdict is written as it arrives: a run stopped midway resumes where it stopped")
    print("--limit N caps the calls per judge; --request prints the first request as it would be sent")
    if request:
        first = next(c for cs in todo.values() for c in cs)
        print(f"\nthe first request, {judges[0]} on {first.fingerprint}:")
        print(json.dumps(_core.judge_request(first, prompt, judges[0]), indent=1))


def run_judge(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Each judge's verdict on every cluster of the span that lacks one; a dry run without --judge."""
    days = _core.span_of(ap, args)
    prompt = _core.judge_prompt()
    settings = _core.judge_settings() if args.judge else None
    judges = args.model or (settings.models if settings else _core.judge_models())
    by_tier = _core.tiers()
    _core.require_models({*judges, *(m for ms in by_tier.values() for m in ms)})
    _core.require_private(judges)
    # The span as the next commands should name it, from the parsed values.
    if args.start or args.end:
        span_args = f"--from {days[0]} --to {days[-1]}"
    else:
        span_args = f"--days {args.days}" if args.days != 7 else ""
    store = _open(args)
    try:
        clusters = list(store.clusters_over(days))
        todo = {
            model: [c for c in store.stale(clusters, prompt.hash, model) if c.who != _core.MASTER_KEY][: args.limit]
            for model in judges
        }
        if not args.judge:
            _dry_run(todo, judges, days, prompt, request=args.request)
            return 0
        tally: dict[str, collections.Counter] = {m: collections.Counter() for m in judges}
        for model, cs in todo.items():
            for i, c in enumerate(cs, 1):
                try:
                    verdict = _core.parse_verdict(_core.ask(settings, _core.judge_request(c, prompt, model)))
                except _core.JudgeError as e:
                    raise SystemExit(f"{model} on {c.fingerprint}: {e}") from None
                store.write_verdict(c, prompt.hash, model, verdict)
                tally[model]["verdicts"] += 1
                tally[model][f"asf {verdict.asf_work}"] += 1
                tally[model][f"tier {verdict.tier}"] += 1
                print(f"{model} {i}/{len(cs)} {c.fingerprint} {_said(verdict)}")
    finally:
        store.close()

    print()
    for model, counts in tally.items():
        n = counts.pop("verdicts", 0)
        said = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        print(f"{model}: {fmt.plural(n, 'verdict')} this run" + (f" ({said})" if n else ""))
    print("\nnext:")
    nexts = [
        (f"  prompts {command} {span_args}".rstrip(), what)
        for command, what in (
            ("verdicts", "the consensus, marked where the judges split"),
            ("verdicts --compare --reasons", "each judge's answers and reasons"),
            ("tiers", "what a cheaper model would have served"),
        )
    ]
    width = max(len(line) for line, _ in nexts) + 2
    for line, what in nexts:
        print(line.ljust(width) + f"# {what}")
    return 0


# ---- verdicts -------------------------------------------------------------


def _verdicts(clusters: list, days: list[dt.date], *, compare: bool, reasons: bool) -> None:
    smoke = [c for c in clusters if c.who == _core.MASTER_KEY]
    judged = [c for c in clusters if c.verdict and c.who != _core.MASTER_KEY]
    judges = sorted({v.model for c in judged for v in c.verdicts})
    print(
        f"{fmt.plural(len(clusters), 'cluster')} over {_core.span_text(days)}, {len(judged)} with a verdict; "
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
        judges_n = v.judges if v else None
        rows.append([c.fingerprint, c.project, c.who, c.tool, c.requests, judges_n, *answers, _core.models_text(c)])
        rows[-1].append(_core.opening_text(c, OPENING_SHOWN))
    _core.table(
        "Verdicts: the judges' consensus",
        ["fingerprint", "project", "who", "tool", "requests", "judges", "asf", "tier", "activity", "models", "opening"],
        rows,
    )
    if split:
        print("* the judges did not all say the same; --compare shows each one's answer.\n")

    if compare and judges:
        differ = [c for c in judged if len({_said(v) for v in c.verdicts}) > 1]
        _core.table(
            f"Where the judges differ ({len(differ)} of {len(judged)} judged)",
            ["fingerprint", "project", "who", "requests", *judges],
            [
                [c.fingerprint, c.project, c.who, c.requests]
                + [_said(next((v for v in c.verdicts if v.model == m), None)) for m in judges]
                for c in differ
            ],
        )

    if reasons:
        for c in judged:
            print(c.fingerprint)
            for v in c.verdicts:
                print(f"  {v.model}: {v.reason}")
        print()
    if smoke:
        print(f"[1] {fmt.plural(len(smoke), 'cluster')} from the master key: the operators' smoke tests, not judged.")


def run_verdicts(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    days = _core.span_of(ap, args)
    clusters = _consensus(args, days, _core.judge_prompt().hash)
    if args.json:
        json.dump(clusters, sys.stdout, indent=1)
        sys.stdout.write("\n")
    else:
        _verdicts(clusters, days, compare=args.compare, reasons=args.reasons)
    return 0


# ---- tiers ----------------------------------------------------------------


def run_tiers(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    days = _core.span_of(ap, args)
    by_tier = _core.tiers()
    _core.require_models({m for ms in by_tier.values() for m in ms})
    clusters = _consensus(args, days, _core.judge_prompt().hash)
    judged = [c for c in clusters if c.verdict and c.who != _core.MASTER_KEY]
    report = [r for r in _core.tier_report(judged, by_tier) if r.above or r.below or r.unranked]
    print(f"{fmt.plural(len(judged), 'judged cluster')} over {_core.span_text(days)}, {len(report)} off their tier\n")
    _core.table(
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
                r.cluster.requests,
                r.tier,
                r.recommended,
                _core.models_text(r.cluster),
                r.above or None,
                r.below or None,
                r.unranked or None,
            ]
            for r in report
        ],
    )
    print(
        "heavier: requests on a model above the consensus tier, which a cheaper model would have served; "
        "lighter: below it; unranked: a model the tier map does not place (config.yaml analysis_tiers)."
    )
    return 0

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

    prompts judge                       # dry run: the first request it would send, what each judge still lacks
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
tests and are never judged. A judge must run only on our boxes.

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


def _open(args: argparse.Namespace) -> _core.Store:
    return _core.Store(args.analysis_dir or _core.analysis_dir())


def _consensus(args: argparse.Namespace, days: list[dt.date], prompt_hash: str) -> list:
    store = _open(args)
    try:
        return list(store.with_consensus(store.clusters_over(days), prompt_hash))
    finally:
        store.close()


# ---- judge ----------------------------------------------------------------


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
            first = next((cs[0] for cs in todo.values() if cs), None)
            if first:
                print(json.dumps(_core.judge_request(first, prompt, judges[0]), indent=1))
                print()
            counts = ", ".join(f"{m} {len(cs)}" for m, cs in todo.items())
            print(f"clusters still to judge over {_core.span_text(days)}: {counts}")
            print("nothing sent without --judge")
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
    for command, what in (
        ("verdicts", "the consensus, marked where the judges split"),
        ("verdicts --compare --reasons", "each judge's answers and reasons"),
        ("tiers", "what a cheaper model would have served"),
    ):
        print(f"  prompts {command} {span_args}".rstrip().ljust(56) + f"# {what}")
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
        "heavier: requests on a model above the consensus tier, which a cheaper model would have served; "
        "lighter: below it; unranked: a model the tier map does not place (config.yaml analysis_tiers)."
    )
    return 0

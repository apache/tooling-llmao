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

"""The store's clusters: build them from the day files, list them, show one.

    prompts build                  # read the last 7 closed days into the store, those not yet in
    prompts build --rebuild        # forget the days and clusters, read the span again
    prompts clusters               # who is sending what kind of request, over the span
    prompts clusters --json
    prompts show 1f2e3d4c5b6a7980  # one cluster, exemplar in full

A cluster is the set of records sharing a fingerprint: the project, the
person, the tool family and the opening of the system prompt. build reads
each day of the span the store has not seen, prunes what is past the
retention, and writes the clusters and their figures: a day is read once.
clusters and show only read the store. No model is involved in any of
this; that is `prompts judge`.

The store is analysis_dir in config.yaml; the log directory is
--log-dir, else prompt_log_dir in config.yaml.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

import _core

from llmao import fmt

if TYPE_CHECKING:
    import argparse
    import datetime as dt


def _list(clusters: list, days: list[dt.date]) -> None:
    total = sum(c.requests for c in clusters)
    print(f"{fmt.plural(len(clusters), 'cluster')}, {fmt.plural(total, 'request')} over {_core.span_text(days)}\n")
    rows = []
    for c in clusters:
        ok = max(1, c.requests - c.failures)
        figures = [c.requests, c.days, c.prompt_tokens // ok, c.completion_tokens // ok]
        rows.append([c.fingerprint, c.project, c.who, c.tool, *figures, _core.models_text(c), _core.opening_text(c)])
    _core.table(
        "Clusters",
        ["fingerprint", "project", "who", "tool", "requests", "days", "prompt/req", "compl/req", "models", "opening"],
        rows,
    )


def _show(store: _core.Store, fp: str) -> int:
    c = store.cluster(fp)
    if c is None:
        print(f"no cluster {fp}", file=sys.stderr)
        return 1
    print(f"cluster {c.fingerprint}: project {c.project}, {c.who}, tool {c.tool}")
    print(f"seen {c.first_seen} to {c.last_seen}, {fmt.plural(c.records, 'record')} in the store")
    print(f"\nopening ({c.source}):\n  {c.opening or '(no text)'}")
    if c.exemplar_json:
        print(
            f"\nexemplar: call {c.exemplar_call_id}, picked {c.exemplar_picked} "
            f"at {fmt.plural(c.exemplar_records, 'record')}, hash {c.exemplar_hash}"
        )
        print(f"\n  head (first {_core.HEAD_CHARS} characters):\n  {c.exemplar_head}")
        for i, m in enumerate(json.loads(c.exemplar_json), 1):
            print(f"\n  [{i}] {m}")
    else:
        print("\nno exemplar: no user message of text in any of its records")
    return 0


def _open(args: argparse.Namespace) -> _core.Store:
    return _core.Store(args.analysis_dir or _core.analysis_dir())


def run_build(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Read the span's day files the store has not seen, prune, and say what is in the store now."""
    days = _core.span_of(ap, args)
    store = _open(args)
    try:
        if args.rebuild:
            store.rebuild()
        gone = store.prune(_core.retention_days())
        log_dir = args.log_dir or _core.log_dir()
        scanned = store.scanned_days()
        read = 0
        for day in days:
            if day in scanned:
                continue
            n = store.scan_day(log_dir, day)
            read += 1
            print(f"read {day}: {fmt.plural(n, 'record')}")
        if gone:
            print(f"pruned {fmt.plural(gone, 'cluster')} past the retention")
        clusters = store.clusters_over(days)
        total = sum(1 for _ in clusters)
    finally:
        store.close()
    print(f"{fmt.plural(read, 'day')} read; {fmt.plural(total, 'cluster')} over {_core.span_text(days)} in the store")
    print("\nnext: prompts clusters, to see them; prompts judge, for the judges' verdicts on those that lack one")
    return 0


def run_clusters(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    days = _core.span_of(ap, args)
    store = _open(args)
    try:
        clusters = list(store.clusters_over(days))
    finally:
        store.close()
    if args.json:
        json.dump(clusters, sys.stdout, indent=1)
        sys.stdout.write("\n")
    else:
        _list(clusters, days)
    return 0


def run_show(ap: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    store = _open(args)
    try:
        return _show(store, args.fingerprint)
    finally:
        store.close()

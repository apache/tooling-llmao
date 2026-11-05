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

"""What the analysis scripts share: the settings, the days, the clusters and their store.

The analyses of the prompt log are R&D and live in this directory, outside
the llmao package (docs/prompt-logging-design.md, "The post-process"):
nothing in llmao/ or bin/ imports from here. The scripts import
llmao.prompt_log for the records themselves and this module for what they
need around them.

config.yaml is read here, from the current directory, because a script runs
at a shell on the gateway host with no portal around it. The portal reads
its config once in main.py and nothing in llmao/ opens the file.

Three words, kept apart. A FINGERPRINT is a hash key over a record: the
project, the person, the tool family and the opening of the system prompt.
A CLUSTER is the set of records sharing a fingerprint, which is pure string
work over the day files, no model involved. An ACTIVITY is what the people
behind a cluster were doing, which a model is asked to say (the judge, a
later script) and which is never the cluster's name.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import sqlite3
from typing import TYPE_CHECKING, Any

from easydict import EasyDict as edict  # noqa: N813

from llmao.prompt_log import NO_PROJECT, counts_as_demand, percentile, read_day, tool_family

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# ---- settings -----------------------------------------------------------

CONFIG = pathlib.Path("config.yaml")
DEFAULT_ANALYSIS_DIR = pathlib.Path("analysis-data")
DEFAULT_RETENTION_DAYS = 90


def _settings(config: pathlib.Path) -> dict:
    if not config.exists():
        raise SystemExit(f"{config}: not found; run from the checkout, or give the path on the command line")
    import yaml

    return yaml.safe_load(config.read_text()) or {}


def configured(key: str, config: pathlib.Path = CONFIG) -> pathlib.Path:
    """A required path from config.yaml in the current directory.

    A missing file or key stops the script with a message. A script can
    fail harder than the portal: a startup error there takes a service
    down, here it makes the admin fix their invocation.
    """
    raw = _settings(config).get(key)
    if not raw:
        raise SystemExit(f"{config}: {key} is required")
    return pathlib.Path(str(raw))


def log_dir(config: pathlib.Path = CONFIG) -> pathlib.Path:
    """prompt_log_dir: where the proxy's hook writes the day files."""
    return configured("prompt_log_dir", config)


# The analysis settings are optional with defaults until p6 renders them
# into config.yaml; then they become required like prompt_log_dir.


def analysis_dir(config: pathlib.Path = CONFIG) -> pathlib.Path:
    """analysis_dir: where the scripts keep their store; ./analysis-data until p6 renders it."""
    raw = _settings(config).get("analysis_dir")
    return pathlib.Path(str(raw)) if raw else DEFAULT_ANALYSIS_DIR


def retention_days(config: pathlib.Path = CONFIG) -> int:
    """analysis_retention_days: how long every row of the store is kept.

    One value for all analysis output. Falls back to the prompt log's
    retention, since the store holds text from the log and should not
    outlive it, and then to 90 days.
    """
    s = _settings(config)
    return int(s.get("analysis_retention_days") or s.get("prompt_log_retention_days") or DEFAULT_RETENTION_DAYS)


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


# ---- the fingerprint ----------------------------------------------------
#
# The OPENING of the system prompt: its first sentence or line, whichever
# ends first, at most OPENING_CHARS after whitespace is collapsed. Not more,
# because a prompt template carries its variable part early ("Extract an
# inventory from each file below. ## Domain: ...") and a longer cut makes a
# cluster per file; not the end, because agents append the working directory
# and the date there. The person rather than the key, because a person
# evaluating models holds one key per model and the work is the same.
# Changing OPENING_CHARS or the boundaries changes every fingerprint, so
# they are set once. The hash is blake2b at 8 bytes: fast, stable across
# runs, and a clash would only merge two clusters, which is no harm.
#
# The HEAD, the first HEAD_CHARS of the same text, is kept with the exemplar
# for a person or the judge to read; it is not part of the key.

OPENING_CHARS = 200
HEAD_CHARS = 1024
_WS = re.compile(r"\s+")
_BOUNDARY = re.compile(r"[.?!](?=\s)|\n")


def digest(*parts: str) -> str:
    h = hashlib.blake2b(digest_size=8)
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


def normalised(text: str) -> str:
    return _WS.sub(" ", text).strip()


def message_text(content: Any) -> str | None:
    """A message's text: the string, or its text parts; None when it has none or carries anything else.

    A user message with an image, a tool result or a tool call is not a
    question a person typed, so it is not text for the head or an exemplar.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            return None
        texts.append(part.get("text") or "")
    return "\n".join(texts) if texts else None


def instructions(record: dict) -> tuple[str, str]:
    """The text that says what the request is for, raw, and where it came from.

    The first system (or developer) message; else the Anthropic API's
    ``system`` parameter, which the record keeps under ``params``; else
    the opening user message. ``("none", "")`` when the record has no
    text at all, which still clusters: a tool that sends only images, say.
    """
    messages = record.get("messages")
    messages = messages if isinstance(messages, list) else []
    for m in messages:
        if m.get("role") in ("system", "developer"):
            text = message_text(m.get("content"))
            if text:
                return "system", text
            break
    system = (record.get("params") or {}).get("system")
    if system:
        text = system if isinstance(system, str) else message_text(system)
        if text:
            return "param", text
    for m in messages:
        if m.get("role") == "user":
            text = message_text(m.get("content"))
            if text:
                return "user", text
            break
    return "none", ""


def opening(text: str) -> str:
    """The first sentence or line, whichever ends first, whitespace collapsed, at most OPENING_CHARS."""
    cut = _BOUNDARY.search(text.lstrip())
    first = text.lstrip()[: cut.end()] if cut else text
    return normalised(first)[:OPENING_CHARS]


def head(text: str) -> str:
    return normalised(text)[:HEAD_CHARS]


def who(key: dict) -> str:
    """The person behind a key, else the key's alias, else the start of its hash: never empty."""
    return key.get("user") or key.get("alias") or (key.get("hash") or "?")[:12]


def fingerprint(record: dict) -> edict | None:
    """The record's fingerprint with the parts it is made of; None when the record is not demand."""
    key = record.get("key") or {}
    if not counts_as_demand(record.get("model"), key.get("project")):
        return None
    project = key.get("project") or NO_PROJECT
    tool = tool_family(record.get("user_agent"))
    source, text = instructions(record)
    start = opening(text)
    return edict(
        fingerprint=digest(project, who(key), tool, start),
        project=project,
        who=who(key),
        tool=tool,
        source=source,
        opening=start,
    )


# ---- the exemplar -------------------------------------------------------
#
# One per cluster, for a person or the judge to read: the head of one
# record's instructions and up to EXEMPLAR_MESSAGES user messages, chosen
# from the cluster's records. Every step is deterministic, so the same
# records give the same exemplar; it is chosen when the cluster is first
# seen and re-picked only when the cluster has at least doubled since the
# pick, so a daily run does not change it and a judge's verdict stays good.

EXEMPLAR_MESSAGES = 3
MESSAGE_CHARS = 1500
MIN_MESSAGE_CHARS = 20


def last_user_text(record: dict) -> str | None:
    """The last user message that is text: skipping tool results and images on the way back."""
    messages = record.get("messages")
    for m in reversed(messages if isinstance(messages, list) else []):
        if m.get("role") == "user":
            text = message_text(m.get("content"))
            if text:
                return normalised(text)
    return None


def prompt_tokens(record: dict) -> int:
    return (record.get("tokens") or {}).get("prompt") or 0


def completion_tokens(record: dict) -> int:
    return (record.get("tokens") or {}).get("completion") or 0


def candidate(record: dict, text: str) -> edict | None:
    """What the exemplar choice needs from one record, so a day is not held in memory whole.

    ``text`` is the record's instructions, already extracted for the
    fingerprint. A record whose last user message is too short to say
    anything ("continue", "y") is not a candidate.
    """
    message = last_user_text(record)
    if not message or len(message) < MIN_MESSAGE_CHARS:
        return None
    return edict(
        tokens=prompt_tokens(record),
        ts=record.get("ts") or "",
        call_id=record.get("call_id") or "",
        head=head(text),
        message=message[:MESSAGE_CHARS],
    )


def choose_exemplar(candidates: Iterable[edict], lengths: list[int]) -> edict | None:
    """Up to EXEMPLAR_MESSAGES distinct messages, the ones most typical of the cluster.

    Ranked by how close the record's prompt length is to the cluster's
    median, earliest first among equals: the typical request, not the
    biggest and not the first, which is often a smoke test. The call id
    and the head are the first message's. The hash covers both and the
    messages.
    """
    median = percentile(lengths, 0.5) or 0
    seen: set[str] = set()
    ranked = []
    for c in candidates:
        h = digest(c.message)
        if h in seen:
            continue
        seen.add(h)
        ranked.append((abs(c.tokens - median), c.ts, c.call_id, c.head, c.message))
    ranked.sort()
    picked = ranked[:EXEMPLAR_MESSAGES]
    if not picked:
        return None
    messages = [p[4] for p in picked]
    first = picked[0]
    return edict(call_id=first[2], head=first[3], messages=messages, hash=digest(first[3], *messages))


# ---- the store ----------------------------------------------------------
#
# One SQLite file in analysis_dir. A day is scanned once: its figures per
# cluster go to cluster_days, the cluster rows carry what does not change
# per day plus the exemplar, and the days table says which days are in.
# Rebuilding drops the clusters and the days, never a judge's verdict.

DB_NAME = "prompts.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    day TEXT PRIMARY KEY,
    scanned_at TEXT NOT NULL,
    records INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS clusters (
    fingerprint TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    who TEXT NOT NULL,
    tool TEXT NOT NULL,
    source TEXT NOT NULL,
    opening TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    records INTEGER NOT NULL,
    exemplar_call_id TEXT,
    exemplar_head TEXT,
    exemplar_json TEXT,
    exemplar_hash TEXT,
    exemplar_records INTEGER NOT NULL DEFAULT 0,
    exemplar_picked TEXT
);
CREATE TABLE IF NOT EXISTS cluster_days (
    fingerprint TEXT NOT NULL,
    day TEXT NOT NULL,
    requests INTEGER NOT NULL,
    failures INTEGER NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    prompt_p50 INTEGER,
    completion_p50 INTEGER,
    models_json TEXT NOT NULL,
    PRIMARY KEY (fingerprint, day)
);
"""


class Store:
    def __init__(self, directory: pathlib.Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / DB_NAME
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def scanned_days(self) -> set[dt.date]:
        return {dt.date.fromisoformat(r["day"]) for r in self.db.execute("SELECT day FROM days")}

    def cluster(self, fp: str) -> edict | None:
        row = self.db.execute("SELECT * FROM clusters WHERE fingerprint = ?", (fp,)).fetchone()
        return row and edict(row)

    def rebuild(self) -> None:
        """Forget every scanned day and cluster. Verdicts, when there are any, stay."""
        with self.db:
            self.db.execute("DELETE FROM cluster_days")
            self.db.execute("DELETE FROM clusters")
            self.db.execute("DELETE FROM days")

    def prune(self, retention: int, today: dt.date | None = None) -> int:
        """Drop every row older than the retention; clusters with no day left go too."""
        cutoff = ((today or dt.datetime.now(dt.UTC).date()) - dt.timedelta(days=retention)).isoformat()
        with self.db:
            self.db.execute("DELETE FROM cluster_days WHERE day < ?", (cutoff,))
            self.db.execute("DELETE FROM days WHERE day < ?", (cutoff,))
            self.db.execute(
                "UPDATE clusters SET records = COALESCE("
                "(SELECT SUM(requests) FROM cluster_days d WHERE d.fingerprint = clusters.fingerprint), 0)"
            )
            gone = self.db.execute("DELETE FROM clusters WHERE records = 0").rowcount
        return gone

    def scan_day(self, log_dir: pathlib.Path, day: dt.date) -> int:
        """Cluster one day's records and write them, one transaction; returns the records clustered."""
        groups: dict[str, edict] = {}
        for r in read_day(log_dir, day):
            fp = fingerprint(r)
            if fp is None:
                continue
            g = groups.get(fp.fingerprint)
            if g is None:
                g = groups[fp.fingerprint] = edict(
                    fp, requests=0, failures=0, prompt=[], completion=[], models={}, candidates=[]
                )
            g.requests += 1
            if r.get("status") == "failure":
                g.failures += 1
            else:
                g.prompt.append(prompt_tokens(r))
                g.completion.append(completion_tokens(r))
            g.models[r["model"]] = g.models.get(r["model"], 0) + 1
            c = candidate(r, instructions(r)[1])
            if c is not None:
                g.candidates.append(c)
        records = sum(g.requests for g in groups.values())
        now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        with self.db:
            for g in groups.values():
                self._write_cluster_day(day, g)
            self.db.execute(
                "INSERT OR REPLACE INTO days (day, scanned_at, records) VALUES (?, ?, ?)",
                (day.isoformat(), now, records),
            )
        return records

    def _write_cluster_day(self, day: dt.date, g: edict) -> None:
        d = day.isoformat()
        self.db.execute(
            "INSERT OR REPLACE INTO cluster_days (fingerprint, day, requests, failures, prompt_tokens, "
            "completion_tokens, prompt_p50, completion_p50, models_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                g.fingerprint,
                d,
                g.requests,
                g.failures,
                sum(g.prompt),
                sum(g.completion),
                percentile(g.prompt, 0.5),
                percentile(g.completion, 0.5),
                json.dumps(g.models, sort_keys=True),
            ),
        )
        row = self.cluster(g.fingerprint)
        if row is None:
            self.db.execute(
                "INSERT INTO clusters (fingerprint, project, who, tool, source, opening, first_seen, last_seen, "
                "records) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (g.fingerprint, g.project, g.who, g.tool, g.source, g.opening, d, d),
            )
        # The cluster's count is the sum of its days, so a rescanned day is
        # not counted twice.
        self.db.execute(
            "UPDATE clusters SET first_seen = MIN(first_seen, ?), last_seen = MAX(last_seen, ?), "
            "records = (SELECT SUM(requests) FROM cluster_days d WHERE d.fingerprint = clusters.fingerprint) "
            "WHERE fingerprint = ?",
            (d, d, g.fingerprint),
        )
        row = self.cluster(g.fingerprint)
        if row.exemplar_hash and row.records < 2 * row.exemplar_records:
            return
        ex = choose_exemplar(g.candidates, g.prompt)
        if ex is None or ex.hash == row.exemplar_hash:
            return
        self.db.execute(
            "UPDATE clusters SET exemplar_call_id = ?, exemplar_head = ?, exemplar_json = ?, exemplar_hash = ?, "
            "exemplar_records = ?, exemplar_picked = ? WHERE fingerprint = ?",
            (ex.call_id, ex.head, json.dumps(ex.messages), ex.hash, row.records, d, g.fingerprint),
        )

    def clusters_over(self, days: list[dt.date]) -> Iterator[edict]:
        """Every cluster with a day in the span, with its figures over the span, most requests first."""
        first, last = days[0].isoformat(), days[-1].isoformat()
        rows = self.db.execute(
            "SELECT c.*, SUM(d.requests) AS requests, SUM(d.failures) AS failures, COUNT(d.day) AS days, "
            "SUM(d.prompt_tokens) AS prompt_tokens, SUM(d.completion_tokens) AS completion_tokens, "
            "GROUP_CONCAT(d.models_json, '\n') AS models_rows "
            "FROM clusters c JOIN cluster_days d USING (fingerprint) WHERE d.day BETWEEN ? AND ? "
            "GROUP BY c.fingerprint ORDER BY requests DESC, c.fingerprint",
            (first, last),
        )
        for row in rows:
            c = edict(row)
            models: dict[str, int] = {}
            for line in (c.pop("models_rows") or "").split("\n"):
                for model, n in json.loads(line or "{}").items():
                    models[model] = models.get(model, 0) + n
            c.models = models
            c.exemplar = json.loads(c.exemplar_json) if c.exemplar_json else []
            yield c

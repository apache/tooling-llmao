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

config.yaml is read here, from the checkout root beside this directory,
because a script runs at a shell on the gateway host with no portal around
it. The portal reads its config once in main.py and nothing in llmao/
opens the file. Relative paths in it are relative to that root, so
``./analysis/prompts`` and ``cd analysis && ./prompts`` read the
same file and the same store.

Three words, kept apart. A FINGERPRINT is a hash key over a record: the
project, the person, the tool family and the opening of the system prompt.
A CLUSTER is the set of records sharing a fingerprint, which is pure string
work over the day files, no model involved. An ACTIVITY is what the people
behind a cluster were doing, which a model is asked to say (the judge, a
later script) and which is never the cluster's name.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import pathlib
import re
import sqlite3
import sys
from typing import TYPE_CHECKING, Any

import httpx
from easydict import EasyDict as edict  # noqa: N813

from llmao.models import load_models, models_path_from_cfg
from llmao.prompt_log import NO_PROJECT, counts_as_demand, percentile, read_day, tool_family

if TYPE_CHECKING:
    import argparse
    from collections.abc import Iterable, Iterator

# ---- settings -----------------------------------------------------------

ROOT = pathlib.Path(__file__).resolve().parent.parent  # the checkout, where config.yaml lives
CONFIG = ROOT / "config.yaml"
DEFAULT_ANALYSIS_DIR = ROOT / "analysis-data"
DEFAULT_RETENTION_DAYS = 90
DEFAULT_MODELS = ["qwen3.8-27b-int4"]  # the judges; one in these runs, several as the normal operation
DEFAULT_GATEWAY = "https://llm.apache.org"
ANALYSIS_KEY_PURPOSE = "llmao-analysis"  # the judge's service key; its own requests are never clustered
# Which models serve which tier, lowest first within a tier; the first is the
# one to recommend. A guess to be corrected in config.yaml analysis_tiers.
DEFAULT_TIERS = {
    "low": ["qwen3-8b", "qwen3.5-9b"],
    "medium": ["qwen3.8-27b-int4"],
    "high": ["qwen3.8-27b", "gemma4-26b"],
}


def _settings(config: pathlib.Path) -> dict:
    if not config.exists():
        raise SystemExit(f"{config}: not found; give the path on the command line")
    import yaml

    return yaml.safe_load(config.read_text()) or {}


def _path(raw: Any, config: pathlib.Path) -> pathlib.Path:
    """A path from the config, relative ones taken from the config's own directory."""
    return config.resolve().parent / str(raw)


def configured(key: str, config: pathlib.Path = CONFIG) -> pathlib.Path:
    """A required path from config.yaml.

    A missing file or key stops the script with a message. A script can
    fail harder than the portal: a startup error there takes a service
    down, here it makes the admin fix their invocation.
    """
    raw = _settings(config).get(key)
    if not raw:
        raise SystemExit(f"{config}: {key} is required")
    return _path(raw, config)


def log_dir(config: pathlib.Path = CONFIG) -> pathlib.Path:
    """prompt_log_dir: where the proxy's hook writes the day files."""
    return configured("prompt_log_dir", config)


# The analysis settings are optional with defaults until p6 renders them
# into config.yaml; then they become required like prompt_log_dir.


def analysis_dir(config: pathlib.Path = CONFIG) -> pathlib.Path:
    """analysis_dir: where the scripts keep their store; ./analysis-data until p6 renders it."""
    raw = _settings(config).get("analysis_dir")
    return _path(raw, config) if raw else DEFAULT_ANALYSIS_DIR


def retention_days(config: pathlib.Path = CONFIG) -> int:
    """analysis_retention_days: how long every row of the store is kept.

    One value for all analysis output. Falls back to the prompt log's
    retention, since the store holds text from the log and should not
    outlive it, and then to 90 days.
    """
    s = _settings(config)
    return int(s.get("analysis_retention_days") or s.get("prompt_log_retention_days") or DEFAULT_RETENTION_DAYS)


def tiers(config: pathlib.Path = CONFIG) -> dict[str, list[str]]:
    """analysis_tiers: which models serve which tier, else the coded guess."""
    raw = _settings(config).get("analysis_tiers")
    return {t: [str(m) for m in raw.get(t) or []] for t in TIERS} if raw else DEFAULT_TIERS


def require_models(names: set[str], config: pathlib.Path = CONFIG) -> None:
    """Every model a run mentions must be one models.yaml defines, or the script stops.

    The judges, a model to compare with, and every model in the tier map.
    A typo would otherwise be a quiet "no verdicts" or a tier report that
    places nothing. The portal, reading the same config, would warn; a
    script fails.
    """
    known = {m["model_name"] for m in load_models(models_path_from_cfg(edict(_settings(config))))}
    unknown = sorted(names - known)
    if unknown:
        raise SystemExit(f"not in models.yaml: {', '.join(unknown)} (known: {', '.join(sorted(known))})")


def require_private(judges: Iterable[str], config: pathlib.Path = CONFIG) -> None:
    """A judge's prompts are other projects' prompts: a model a paid provider also serves is refused.

    config.yaml providers with an api_base are paid, and the gateway falls
    back to them when our boxes cannot answer; the judge must never be
    the request that goes there. Until the proxy can honour a request's
    own "self-hosted only" (docs/analysis-design.md, the privacy
    mechanism), the guard is here, at startup.
    """
    paid: dict[str, list[str]] = {}
    for name, entry in (_settings(config).get("providers") or {}).items():
        if (entry or {}).get("api_base"):
            for model_name in entry.get("models") or {}:
                paid.setdefault(str(model_name), []).append(name)
    offsite = {j: paid[j] for j in judges if j in paid}
    if offsite:
        said = "; ".join(f"{j} is also served by {', '.join(p)}" for j, p in offsite.items())
        raise SystemExit(f"a judge must run only on our boxes: {said}")


def judge_settings(config: pathlib.Path = CONFIG) -> edict:
    """What the judge needs to call the gateway: its URL, the judges, the key.

    The front door, ``analysis_gateway``, not the proxy's local address:
    the judge is a client like any other, so it runs the same from a
    developer's machine as from the host, and its requests are logged and
    counted like anyone's. ``analysis_models`` is a list: several judges
    on each cluster is the normal operation. It defaults until p6 renders
    it. The key has no default: without ``analysis_key`` the judge stops
    before its first call.
    """
    s = _settings(config)
    key = s.get("analysis_key")
    if not key:
        raise SystemExit(f"{config}: analysis_key is required (a service key whose purpose is {ANALYSIS_KEY_PURPOSE})")
    return edict(
        gateway=str(s.get("analysis_gateway") or DEFAULT_GATEWAY).rstrip("/"),
        models=judge_models(config),
        key=str(key),
    )


def judge_models(config: pathlib.Path = CONFIG) -> list[str]:
    """analysis_models: the judges, in the order they run; the coded default until p6 renders it."""
    raw = _settings(config).get("analysis_models")
    if raw is not None and not isinstance(raw, list):
        raise SystemExit(f"{config}: analysis_models should be a list of model names")
    return [str(m) for m in raw] if raw else list(DEFAULT_MODELS)


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


MASTER_KEY_HASH = "litellm_proxy_master_key"  # LiteLLM's placeholder: no team, user default_user_id
MASTER_KEY = "(master key)"  # the operators' own requests: smoke tests, never judged


def who(key: dict) -> str:
    """The person behind a key, else the key's purpose, else the start of its hash: never empty.

    The purpose is the name typed when the key was made, so a service
    key reads as "tooling-gofannon" rather than a hash. The master key is
    named as such: LiteLLM files it under a placeholder user, and
    "default_user_id" in a listing reads as a person.
    """
    if key.get("hash") == MASTER_KEY_HASH:
        return MASTER_KEY
    return key.get("user") or key.get("purpose") or (key.get("hash") or "?")[:12]


def fingerprint(record: dict) -> edict | None:
    """The record's fingerprint with the parts it is made of; None when the record is not demand."""
    key = record.get("key") or {}
    if not counts_as_demand(record.get("model"), key.get("project")) or key.get("purpose") == ANALYSIS_KEY_PURPOSE:
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
    paired INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (fingerprint, day)
);
CREATE TABLE IF NOT EXISTS verdicts (
    fingerprint TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    exemplar_hash TEXT NOT NULL,
    judged_at TEXT NOT NULL,
    asf_work INTEGER NOT NULL,
    tier INTEGER NOT NULL,
    activity TEXT NOT NULL,
    verdict_json TEXT NOT NULL,
    PRIMARY KEY (fingerprint, prompt_hash, model)
);
"""

# The answers as the judge gives them, in words; stored as their index
# here, so the views aggregate integers: Foundation work ordered so MIN
# leans towards "no", the tier so MAX leans towards "high".
ASF_WORK = ("no", "unclear", "yes")
TIERS = ("low", "medium", "high")

# Rebuilt at every open, so a change here reaches an existing store.
_VIEWS = """
DROP VIEW IF EXISTS consensus;
DROP VIEW IF EXISTS current_verdicts;
-- The verdicts that still hold: the exemplar they were given is the
-- cluster's current one.
CREATE VIEW current_verdicts AS
SELECT v.fingerprint, v.prompt_hash, v.model, v.judged_at, v.asf_work, v.tier, v.activity,
       json_extract(v.verdict_json, '$.reason') AS reason
FROM verdicts v JOIN clusters c USING (fingerprint)
WHERE v.exemplar_hash = c.exemplar_hash;
-- What the judges together say about a cluster under one judge prompt.
-- The tier leans high: the highest any judge gave. Foundation work leans
-- the other way: one "no" is a "no", for an admin to look at. The activity
-- is the majority, the alphabetically first among equals.
CREATE VIEW consensus AS
WITH activities AS (
    SELECT fingerprint, prompt_hash, activity,
           ROW_NUMBER() OVER (PARTITION BY fingerprint, prompt_hash ORDER BY COUNT(*) DESC, activity) AS rank
    FROM current_verdicts GROUP BY fingerprint, prompt_hash, activity
)
SELECT v.fingerprint, v.prompt_hash, COUNT(*) AS judges,
       MIN(v.asf_work) AS asf_work,
       MAX(v.tier) AS tier,
       (SELECT activity FROM activities a
        WHERE a.fingerprint = v.fingerprint AND a.prompt_hash = v.prompt_hash AND a.rank = 1) AS activity,
       COUNT(DISTINCT v.asf_work) AS asf_answers,
       COUNT(DISTINCT v.tier) AS tier_answers,
       COUNT(DISTINCT v.activity) AS activity_answers
FROM current_verdicts v GROUP BY v.fingerprint, v.prompt_hash;
"""


class Store:
    def __init__(self, directory: pathlib.Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / DB_NAME
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        # No migrations while this is R&D: a store whose tables are out of
        # date is deleted and rebuilt. The views are recreated at every
        # open, so a change to them reaches a store without that.
        self.db.executescript(_SCHEMA)
        self.db.executescript(_VIEWS)

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
            self.db.execute("DELETE FROM verdicts WHERE fingerprint NOT IN (SELECT fingerprint FROM clusters)")
        return gone

    def scan_day(self, log_dir: pathlib.Path, day: dt.date) -> int:
        """Cluster one day's records and write them, one transaction; returns the records clustered."""
        groups: dict[str, edict] = {}
        # The same messages sent to several models is a person comparing
        # models, which the judge cannot see from one exemplar: per group,
        # which models each distinct request went to.
        sent_to: dict[str, dict[str, set[str]]] = {}
        for r in read_day(log_dir, day):
            fp = fingerprint(r)
            if fp is None:
                continue
            g = groups.get(fp.fingerprint)
            if g is None:
                g = groups[fp.fingerprint] = edict(
                    fp, requests=0, failures=0, prompt=[], completion=[], models={}, candidates=[], paired=0
                )
                sent_to[fp.fingerprint] = {}
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
            request = digest(json.dumps(r.get("messages"), sort_keys=True))
            sent_to[fp.fingerprint].setdefault(request, set()).add(r["model"])
        for fp, requests in sent_to.items():
            groups[fp].paired = sum(len(models) for models in requests.values() if len(models) > 1)
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
            "completion_tokens, prompt_p50, completion_p50, models_json, paired) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                g.paired,
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
            "SUM(d.paired) AS paired, GROUP_CONCAT(d.models_json, '\n') AS models_rows "
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

    # ---- verdicts -------------------------------------------------------

    def stale(self, clusters: Iterable[edict], prompt_hash: str, model: str) -> list[edict]:
        """The clusters with an exemplar and no verdict from this judge prompt, this model and this exemplar."""
        out = []
        for c in clusters:
            if not c.exemplar_hash:
                continue
            row = self.db.execute(
                "SELECT exemplar_hash FROM verdicts WHERE fingerprint = ? AND prompt_hash = ? AND model = ?",
                (c.fingerprint, prompt_hash, model),
            ).fetchone()
            if row is None or row["exemplar_hash"] != c.exemplar_hash:
                out.append(c)
        return out

    def write_verdict(self, c: edict, prompt_hash: str, model: str, verdict: dict) -> None:
        """One verdict, one transaction: a run that stops keeps every verdict before the stop."""
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO verdicts (fingerprint, prompt_hash, model, exemplar_hash, judged_at, "
                "asf_work, tier, activity, verdict_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    c.fingerprint,
                    prompt_hash,
                    model,
                    c.exemplar_hash,
                    dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                    ASF_WORK.index(verdict["asf_work"]),
                    TIERS.index(verdict["tier"]),
                    verdict["activity"],
                    json.dumps(verdict),
                ),
            )

    @staticmethod
    def _named(row: sqlite3.Row | None) -> edict | None:
        """A verdict row with its integers named again."""
        if row is None:
            return None
        v = edict(row)
        v.asf_work = ASF_WORK[v.asf_work]
        v.tier = TIERS[v.tier]
        return v

    def with_verdicts(self, clusters: Iterable[edict], prompt_hash: str, model: str) -> Iterator[edict]:
        """Each cluster with one model's current verdict under ``verdict`` (None when there is none or it is stale)."""
        for c in clusters:
            row = self.db.execute(
                "SELECT asf_work, tier, activity, reason, judged_at FROM current_verdicts "
                "WHERE fingerprint = ? AND prompt_hash = ? AND model = ?",
                (c.fingerprint, prompt_hash, model),
            ).fetchone()
            c.verdict = self._named(row)
            yield c

    def with_consensus(self, clusters: Iterable[edict], prompt_hash: str) -> Iterator[edict]:
        """Each cluster with what its judges together say under ``verdict``, and every judge's say under ``verdicts``.

        ``verdict`` carries ``judges`` and how many distinct answers each
        question got, so a listing can show where the judges disagreed.
        """
        for c in clusters:
            row = self.db.execute(
                "SELECT * FROM consensus WHERE fingerprint = ? AND prompt_hash = ?", (c.fingerprint, prompt_hash)
            ).fetchone()
            c.verdict = self._named(row)
            c.verdicts = [
                self._named(r)
                for r in self.db.execute(
                    "SELECT model, asf_work, tier, activity, reason FROM current_verdicts "
                    "WHERE fingerprint = ? AND prompt_hash = ? ORDER BY model",
                    (c.fingerprint, prompt_hash),
                )
            ]
            yield c


# ---- the judge ----------------------------------------------------------
#
# One call per cluster: the judge prompt as the system message, the
# exemplar and the cluster's figures as the user message, a JSON object
# back. The judge prompt is a file, so rewording it is not a code change;
# its hash, the exemplar's hash and the model name are what make a stored
# verdict stale. The model runs on our own boxes: the exemplars are other
# projects' prompts and do not leave them.

JUDGE_PROMPT = pathlib.Path(__file__).resolve().parent / "judge-prompt.md"
ACTIVITIES = (
    "security-review",
    "code-review",
    "feature-or-bugfix",
    "model-evaluation",
    "classification-or-extraction",
    "writing-or-documentation",
    "conversation",
    "other",
)


def judge_prompt(path: pathlib.Path = JUDGE_PROMPT) -> edict:
    text = path.read_text(encoding="utf-8")
    return edict(text=text, hash=digest(text))


def exemplar_message(c: edict) -> str:
    """The user message for one cluster: its figures, its head, its messages."""
    ok = max(1, c.requests - c.failures)
    models = ", ".join(sorted(c.models))
    paired = (
        f"{c.paired:,} of the requests were the same request sent to more than one model"
        if c.paired
        else "no request was sent to more than one model"
    )
    lines = [
        f"Project: {c.project}",
        f"Tool: {c.tool}",
        f"Requests: {c.requests:,} over {c.days} day(s); mean prompt {c.prompt_tokens // ok:,} tokens; "
        f"mean completion {c.completion_tokens // ok:,} tokens; models used: {models}; {paired}",
        "",
        f"INSTRUCTIONS (first {HEAD_CHARS} characters):",
        c.exemplar_head or "(none)",
        "",
        f"USER MESSAGES (up to {EXEMPLAR_MESSAGES}, each cut to {MESSAGE_CHARS} characters):",
    ]
    lines.extend(f"[{i}] {m}" for i, m in enumerate(c.exemplar, 1))
    return "\n".join(lines)


def judge_request(c: edict, prompt: edict, model: str) -> dict:
    """The chat completion body for one cluster. Reasoning off: the answer is a short JSON object."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt.text},
            {"role": "user", "content": exemplar_message(c)},
        ],
        "temperature": 0.0,
        "max_tokens": 600,
        "chat_template_kwargs": {"enable_thinking": False},
    }


class JudgeError(Exception):
    """The gateway did not answer, or the answer was not a verdict."""


def ask(settings: edict, body: dict, timeout: float = 120.0) -> str:
    """One chat completion through the gateway, synchronously; the assistant's text, or JudgeError.

    One request in flight is the whole point, so the synchronous httpx
    client, not the async one the portal's transport uses.
    """
    try:
        r = httpx.post(
            settings.gateway + "/v1/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {settings.key}"},
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise JudgeError(f"{type(e).__name__}: {e}"[:200]) from None
    if r.status_code != 200:
        raise JudgeError(f"HTTP {r.status_code} {r.text[:200]}".replace("\n", " "))
    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError, ValueError):
        raise JudgeError(f"no message in the answer: {r.text[:200]}") from None


def parse_verdict(text: str) -> edict:
    """The JSON object in the judge's answer, checked against the three answer sets.

    Lenient about what surrounds the object, since a small model will
    sometimes add a fence or a sentence despite being told not to; strict
    about the values, since a verdict outside the sets is not a verdict.
    """
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise JudgeError(f"no JSON object in the answer: {text[:200]!r}")
    try:
        v = json.loads(text[start : end + 1])
    except ValueError:
        raise JudgeError(f"the answer is not JSON: {text[:200]!r}") from None
    for key, allowed in (("asf_work", ASF_WORK), ("tier", TIERS), ("activity", ACTIVITIES)):
        if v.get(key) not in allowed:
            raise JudgeError(f"{key} is {v.get(key)!r}, not one of {allowed}")
    return edict(asf_work=v["asf_work"], tier=v["tier"], activity=v["activity"], reason=str(v.get("reason") or ""))


# ---- the tier report ----------------------------------------------------
#
# The judge's tier against the models the cluster actually used. A request
# on a model above the tier is one a cheaper model would have served; one
# below is a risk the judge thinks the model is not up to.


def tier_of(model: str, by_tier: dict[str, list[str]]) -> str | None:
    """The tier a model serves, or None for a model the map does not know (a paid provider's, say)."""
    for tier, models in by_tier.items():
        if model in models:
            return tier
    return None


def tier_report(clusters: Iterable[edict], by_tier: dict[str, list[str]]) -> list[edict]:
    """Per judged cluster: the tier, the model to recommend, and the requests above and below the tier.

    Heaviest excess first, so the top of the list is the biggest saving.
    """
    rank = {t: i for i, t in enumerate(TIERS)}
    out = []
    for c in clusters:
        if not c.verdict:
            continue
        verdict_rank = rank[c.verdict.tier]
        above = below = unranked = 0
        for model, n in c.models.items():
            t = tier_of(model, by_tier)
            if t is None:
                unranked += n
            elif rank[t] > verdict_rank:
                above += n
            elif rank[t] < verdict_rank:
                below += n
        recommended = (by_tier.get(c.verdict.tier) or ["?"])[0]
        out.append(
            edict(cluster=c, tier=c.verdict.tier, recommended=recommended, above=above, below=below, unranked=unranked)
        )
    return sorted(out, key=lambda r: (-r.above, -r.below, r.cluster.fingerprint))


# ---- for the commands ---------------------------------------------------
#
# What every subcommand of analysis/prompts shares once its arguments are
# parsed: the span of days they name.


def span_of(ap: argparse.ArgumentParser, args: argparse.Namespace) -> list[dt.date]:
    try:
        return span(days=args.days, start=args.start, end=args.end)
    except ValueError:
        ap.error("--from is after --to")


def span_text(days: list[dt.date]) -> str:
    return f"{days[0]} to {days[-1]}" if len(days) > 1 else str(days[0])


OPENING_SHOWN = 72


def models_text(c: edict) -> str:
    """A cluster's models with their request counts, most used first."""
    return ", ".join(f"{m} {n}" for m, n in sorted(c.models.items(), key=lambda kv: -kv[1]))


def opening_text(c: edict, shown: int = OPENING_SHOWN) -> str:
    """A cluster's opening for a listing: where it came from, and its first characters."""
    if not c.opening:
        return "(no text)"
    return f"{c.source}: {c.opening[:shown]}" + ("…" if len(c.opening) > shown else "")


def table(title: str, columns: list[str], rows: list[list], *, csv_out: bool = False) -> None:
    """One table, as aligned text with a title, or as CSV with the title as a comment.

    Cells are values, not text: an int gets its thousands separator and
    a numeric column is right-aligned; None prints as nothing.
    """
    cells = [
        [
            ("" if v is None else f"{v:,}" if isinstance(v, int) else f"{v:g}" if isinstance(v, float) else str(v))
            for v in row
        ]
        for row in rows
    ]
    if csv_out:
        out = csv.writer(sys.stdout)
        sys.stdout.write(f"# {title}\n")
        out.writerow(columns)
        out.writerows(cells)
        sys.stdout.write("\n")
        return
    widths = [max(len(c), *(len(r[i]) for r in cells)) if cells else len(c) for i, c in enumerate(columns)]
    numeric = [all(isinstance(r[i], (int, float)) or r[i] is None for r in rows) for i in range(len(columns))]

    def line(values):
        return "  ".join(v.rjust(w) if n else v.ljust(w) for v, w, n in zip(values, widths, numeric, strict=True))

    print(title)
    print(line(columns))
    print(line(["-" * w for w in widths]))
    for row in cells:
        print(line(row))
    print()

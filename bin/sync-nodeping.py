#!/usr/bin/env -S uv run --script
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

"""Make NodePing's checks match the fleet's vLLM servers.

    ./bin/sync-nodeping.py [--dry-run] [-v] < document.json
    ./bin/sync-nodeping.py --settings nodeping.yaml --servers servers.json --dry-run

The portal runs this (config.yaml fleet.servers_hook) with one JSON document
on stdin whenever a server's address changes, and that document is the only
input: the hook's settings from config.yaml, and every server with where it
is dialed. Nothing here reads config.yaml, asks a provider, or imports the
portal, so the portal knows nothing of NodePing beyond a script to run.

    {"settings": {"api_key": "...", "prefix": "rai"},
     "servers": [{"model": "qwen3-8b", "host": "58458812", "listen_port": 8001,
                  "provider": "Vast", "health_url": "http://203.0.113.5:18490/health"}, ...]}

One NodePing HTTP check per server, on its /health (vLLM answers it without a
key), labelled "<prefix> <model> on <host>:<listen_port>" -- the server's
identity in fleet.hosts, which an address change does not touch. Every check
in the account is ours: a check whose label names no server is deleted, a
server with no check is given one, a check whose target is not the server's
address is pointed at it. A server with no address yet gets no check; a
server that has lost its address keeps the check it had, which then alerts,
as a rental that has gone should. Hand edits to a check's interval, timeout
or notifications survive: only the target is synchronized. A new check
notifies every Slack address the account has, with the sample check's
parameters (CHECK).

Exit 0 when NodePing matches the servers (or would, with --dry-run); 1 when
NodePing refused or failed a call; 2 when the input is not usable. The last
line is the summary the portal logs.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import httpx
import yaml
from easydict import EasyDict as edict  # noqa: N813

API = "https://api.nodeping.com/api/1"

# The shape of a new check, copied from the first check made by hand: an
# HTTP GET every 5 minutes, 5 seconds to answer, down after 5 failures in a
# row. NodePing takes enabled as the string "true" and reads it back as
# enable: "active".
CHECK = {"type": "HTTP", "interval": 5, "threshold": 5, "sens": 5, "enabled": "true"}
NOTIFY = {"delay": 0, "schedule": "All"}

# What plan() decides for a check or a server, and apply() carries out.
CREATE = "create"  # a server with an address and no check
UPDATE = "update"  # the check's target is not the server's address
DELETE = "delete"  # a check whose label names no server, or a second check with a server's label
KEEP = "keep"  # the server has lost its address; its check stands, and alerts
WAIT = "wait"  # no address yet, no check: nothing to make
UNCHANGED = "unchanged"
# The summary's word for each, in the order the summary lists them.
WORDS = {
    CREATE: "created",
    UPDATE: "updated",
    DELETE: "deleted",
    KEEP: "kept without an address",
    WAIT: "waiting for an address",
    UNCHANGED: "unchanged",
}


class NodePingError(Exception):
    """NodePing refused or failed a call; the message says which."""


class NodePing:
    """The few calls the sync needs. Responses are NodePing's own trees, kept as plain dicts."""

    def __init__(self, token: str, *, http: httpx.Client | None = None):
        self.http = http or httpx.Client(base_url=API, auth=(token, ""), timeout=30.0)

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        # NodePing answers an error with HTTP 200 and {"error": "..."}, so
        # the body is checked as well as the status.
        response = self.http.request(method, path, json=body)
        if response.status_code != 200:
            raise NodePingError(f"{method} {path}: HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        if isinstance(data, dict) and "error" in data:
            raise NodePingError(f"{method} {path}: {data['error']}")
        return data

    def checks(self) -> dict:
        """Every check in the account, by id."""
        return self.call("GET", "/checks")

    def contacts(self) -> dict:
        """Every contact in the account, by id, each with its addresses."""
        return self.call("GET", "/contacts")

    def create_check(self, body: dict) -> dict:
        return self.call("POST", "/checks", body)

    def update_check(self, check_id: str, body: dict) -> dict:
        # A partial PUT keeps every field it does not name. Its response is
        # the check as it was, not as it is, so nothing here reads it.
        return self.call("PUT", f"/checks/{check_id}", body)

    def delete_check(self, check_id: str) -> dict:
        return self.call("DELETE", f"/checks/{check_id}")


def slack_addresses(contacts: dict) -> list[str]:
    """The ids of every Slack address in the account: what a new check notifies."""
    # contacts is NodePing's tree: plain dicts, read with get().
    out = []
    for contact in contacts.values():
        for address_id, address in (contact.get("addresses") or {}).items():
            if address.get("type") == "slack":
                out.append(address_id)
    return sorted(out)


def label_for(prefix: str, srv: edict) -> str:
    """The check's label: the server's identity in fleet.hosts, stable across address changes."""
    return f"{prefix} {srv.model} on {srv.host}:{srv.listen_port}"


def plan(prefix: str, servers: list[edict], checks: dict) -> list[edict]:
    """What to do to each check and for each server, as edict(action, label, check, target, reason). Nothing is sent.

    ``action`` is one of the constants above, CREATE to UNCHANGED.
    """
    wanted = {label_for(prefix, srv): srv for srv in servers}
    by_label: dict[str, list[dict]] = {}
    # checks is NodePing's tree: plain dicts, read with get(). Oldest first,
    # so that of two checks with one label the one made first stands.
    for check in sorted(checks.values(), key=lambda c: c.get("created") or 0):
        by_label.setdefault(str(check.get("label")), []).append(check)
    actions = []
    for label, found in by_label.items():
        extra = found if label not in wanted else found[1:]
        reason = "no such server in fleet.hosts" if label not in wanted else "a second check with this label"
        for check in extra:
            actions.append(edict(action=DELETE, label=label, check=check, target=None, reason=reason))
    for label, srv in wanted.items():
        check = (by_label.get(label) or [None])[0]
        url = srv.health_url
        if url is None:
            action = KEEP if check else WAIT
            actions.append(edict(action=action, label=label, check=check, target=None, reason="no address"))
        elif check is None:
            actions.append(edict(action=CREATE, label=label, check=None, target=url, reason="no check"))
        elif (check.get("parameters") or {}).get("target") != url:
            actions.append(edict(action=UPDATE, label=label, check=check, target=url, reason="address changed"))
        else:
            actions.append(edict(action=UNCHANGED, label=label, check=check, target=url, reason=""))
    return actions


def apply(np: NodePing, prefix: str, actions: list[edict], notify: list[str], *, dry_run: bool, verbose: bool) -> edict:
    """Send each action to NodePing (or say what would be sent) and count them by kind."""
    counts = edict()
    would = "would " if dry_run else ""
    for act in actions:
        counts[act.action] = counts.get(act.action, 0) + 1
        if act.action == UNCHANGED and not verbose:
            continue
        where = f" -> {act.target}" if act.target else ""
        print(f"{would}{act.action} {act.label}{where} ({act.reason})" if act.reason else f"{act.action} {act.label}")
        if dry_run:
            continue
        if act.action == CREATE:
            body = {
                **CHECK,
                "label": act.label,
                "target": act.target,
                "tags": [prefix],
                "notifications": [{address_id: NOTIFY} for address_id in notify],
            }
            _ = np.create_check(body)
        elif act.action == UPDATE:
            _ = np.update_check(act.check["_id"], {"target": act.target})
        elif act.action == DELETE:
            _ = np.delete_check(act.check["_id"])
    return counts


def summary(counts: edict, *, dry_run: bool) -> str:
    """One line for the portal's log: "nodeping: 2 created, 1 deleted, 3 unchanged"."""
    parts = [f"{counts[k]} {word}" for k, word in WORDS.items() if counts.get(k)] or ["nothing to do"]
    return ("nodeping (dry run): " if dry_run else "nodeping: ") + ", ".join(parts)


def read_document(path: str | None) -> edict:
    """The document the portal sends, from a file or stdin; a bare list is taken as the servers."""
    text = pathlib.Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    data = json.loads(text)
    if isinstance(data, list):
        data = {"servers": data}
    return edict(data)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", metavar="YAML", help="api_key and prefix, over those in the document (hand runs)")
    ap.add_argument("--servers", metavar="JSON", help="the document, instead of stdin")
    ap.add_argument("--dry-run", action="store_true", help="say what would change; send nothing")
    ap.add_argument("-v", "--verbose", action="store_true", help="also list the checks that are unchanged")
    args = ap.parse_args(argv)

    document = read_document(args.servers)
    settings = edict(document.get("settings") or {})
    if args.settings:
        settings.update(yaml.safe_load(pathlib.Path(args.settings).read_text(encoding="utf-8")) or {})
    token = str(settings.get("api_key") or "").strip()
    prefix = str(settings.get("prefix") or "").strip()
    if not token or not prefix:
        print("sync-nodeping: settings need api_key and prefix", file=sys.stderr)
        return 2
    servers = [edict(s) for s in document.get("servers") or []]

    np = NodePing(token)
    try:
        notify = slack_addresses(np.contacts())
        if not notify:
            print("sync-nodeping: the NodePing account has no Slack address to notify", file=sys.stderr)
            return 2
        actions = plan(prefix, servers, np.checks())
        counts = apply(np, prefix, actions, notify, dry_run=args.dry_run, verbose=args.verbose)
    except (NodePingError, httpx.HTTPError) as e:
        print(f"sync-nodeping: {e}", file=sys.stderr)
        return 1
    print(summary(counts, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())

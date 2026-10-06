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

"""Requests a person makes and an admin answers.

Two kinds, one shape: someone asks for something they cannot grant
themselves, an admin approves, adjusts or denies with a reason, and the
answer is visible where the asking happened.

  CAPACITY  a PMC wants a larger weekly token allocation. Approving writes
            the new cap to the team.
  KEY       someone wants a key outside a project, or automation that needs
            its own identity. Approving does NOT create the key -- it records
            permission to create one, with fixed parameters, which the
            requester then exercises themselves. The secret is revealed once,
            to them, and never passes through an admin.

WHERE THIS LIVES

In LiteLLM team metadata, beside grantor and token_cap. llmao has no database
of its own and docs/ARCHITECTURE.md states that as a property, so a request
is a row on a team rather than a row in a table llmao owns.

That is the ugly part and it is deliberate: listing everything pending means
reading every team. warm() already enumerates them once per boot, and at
twenty projects the cost is noise. At three hundred it would not be, and the
shape here -- a dataclass with to_dict/from_dict and a store that only knows
how to list, add and replace -- is meant to be liftable into a table without
touching anything that calls it.

A request with no project lives on the reserved entitlement-defaults team,
which already exists for tier overrides.

CONCURRENCY

Writes are read-modify-write on the whole metadata blob. Two admins answering
different requests in the same second can lose one. At pilot volume that is
theoretical; if it stops being theoretical, that is the signal to lift this
into a table rather than to add a lock here.
"""

from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass, field
from typing import Any

# Metadata key on the team. One list, newest last.
METADATA_KEY = "requests"

KIND_CAPACITY = "capacity"
KIND_KEY = "key"

STATE_PENDING = "pending"
STATE_APPROVED = "approved"
STATE_DENIED = "denied"
STATE_CONSUMED = "consumed"

# An approval that nobody acts on is a credential waiting to be forgotten.
APPROVAL_TTL_DAYS = 14


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _parse(stamp: str | None) -> dt.datetime | None:
    if not stamp:
        return None
    try:
        return dt.datetime.fromisoformat(stamp)
    except ValueError:
        return None


@dataclass
class Request:
    """One ask, and its answer.

    `granted` carries what an admin actually approved, which is not always
    what was asked for -- an adjusted capacity bump, or a key at a lower
    allowance than requested. Keeping `wanted` beside it means the record
    still shows the gap, which is the thing a requester wants to see and the
    thing a summary would lose.
    """

    id: str
    kind: str
    requester: str
    project: str | None
    reason: str
    wanted: dict[str, Any] = field(default_factory=dict)
    state: str = STATE_PENDING
    created_at: str = ""
    decided_at: str = ""
    decided_by: str = ""
    # Why an admin adjusted or denied. Shown to the requester: a request that
    # comes back smaller with no explanation teaches people to pad the next
    # one.
    decision_reason: str = ""
    granted: dict[str, Any] = field(default_factory=dict)
    # Set when a key approval is exercised, so it cannot be used twice.
    consumed_at: str = ""

    @classmethod
    def new(cls, *, kind: str, requester: str, project: str | None, reason: str, wanted: dict[str, Any]) -> Request:
        if kind not in (KIND_CAPACITY, KIND_KEY):
            raise ValueError(f"unknown request kind: {kind}")
        requester = (requester or "").strip()
        if not requester:
            raise ValueError("a request needs a requester")
        if kind == KIND_CAPACITY and not project:
            raise ValueError("a capacity request needs a project")
        return cls(
            # Random rather than sequential: ids appear in form posts, and a
            # guessable id is one an admin can be tricked into approving.
            id=secrets.token_urlsafe(9),
            kind=kind,
            requester=requester,
            project=project or None,
            reason=(reason or "").strip(),
            wanted=dict(wanted or {}),
            created_at=_now(),
        )

    # -- state ------------------------------------------------------------

    def expires_at(self) -> dt.datetime | None:
        """When an approval lapses. Only key approvals expire.

        A capacity bump takes effect on approval and has nothing to exercise
        later, so there is nothing to time out.
        """
        if self.kind != KIND_KEY or self.state != STATE_APPROVED:
            return None
        decided = _parse(self.decided_at)
        if decided is None:
            return None
        return decided + dt.timedelta(days=APPROVAL_TTL_DAYS)

    def is_expired(self, *, now: dt.datetime | None = None) -> bool:
        exp = self.expires_at()
        if exp is None:
            return False
        return (now or dt.datetime.now(dt.UTC)) >= exp

    def is_actionable(self, *, now: dt.datetime | None = None) -> bool:
        """An approved key request the requester can still exercise."""
        return (
            self.kind == KIND_KEY
            and self.state == STATE_APPROVED
            and not self.consumed_at
            and not self.is_expired(now=now)
        )

    def decide(self, *, state: str, by: str, reason: str = "", granted: dict[str, Any] | None = None) -> None:
        if self.state != STATE_PENDING:
            raise ValueError(f"request {self.id} is already {self.state}")
        if state not in (STATE_APPROVED, STATE_DENIED):
            raise ValueError(f"cannot decide into {state}")
        # Adjusting or denying without saying why is how a requester learns
        # to pad the next ask.
        #
        # Only a CHANGED value counts as an adjustment. An admin adding a
        # field the requester never named -- the allowance on a projectless
        # service key, which they have no business setting for themselves --
        # is completing the request, not altering it. Demanding a
        # justification for that would train everyone to type "ok" in the
        # box, which costs the reason field its meaning on the decisions that
        # do need one.
        merged = {**self.wanted, **(granted or {})}
        adjusted = any(merged[k] != v for k, v in self.wanted.items() if k in merged)
        if (state == STATE_DENIED or adjusted) and not (reason or "").strip():
            raise ValueError("denying or adjusting a request requires a reason")
        self.state = state
        self.decided_by = by
        self.decided_at = _now()
        self.decision_reason = (reason or "").strip()
        self.granted = merged

    def consume(self) -> None:
        if not self.is_actionable():
            raise ValueError(f"request {self.id} cannot be consumed")
        self.state = STATE_CONSUMED
        self.consumed_at = _now()

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "requester": self.requester,
            "project": self.project,
            "reason": self.reason,
            "wanted": dict(self.wanted),
            "state": self.state,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "decision_reason": self.decision_reason,
            "granted": dict(self.granted),
            "consumed_at": self.consumed_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Request | None:
        """Rehydrate, or None if the row is not a request.

        Returns None rather than raising: metadata is shared with other
        things, and one malformed entry must not cost the whole queue.
        """
        if not isinstance(d, dict) or not d.get("id") or not d.get("kind"):
            return None
        return cls(
            id=str(d["id"]),
            kind=str(d["kind"]),
            requester=str(d.get("requester") or ""),
            project=d.get("project") or None,
            reason=str(d.get("reason") or ""),
            wanted=dict(d.get("wanted") or {}),
            state=str(d.get("state") or STATE_PENDING),
            created_at=str(d.get("created_at") or ""),
            decided_at=str(d.get("decided_at") or ""),
            decided_by=str(d.get("decided_by") or ""),
            decision_reason=str(d.get("decision_reason") or ""),
            granted=dict(d.get("granted") or {}),
            consumed_at=str(d.get("consumed_at") or ""),
        )


def load(metadata: dict[str, Any] | None) -> list[Request]:
    """Requests stored on one team's metadata."""
    raw = (metadata or {}).get(METADATA_KEY)
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        req = Request.from_dict(item)
        if req is not None:
            out.append(req)
    return out


def dump(requests: list[Request]) -> list[dict[str, Any]]:
    return [r.to_dict() for r in requests]


def upsert(requests: list[Request], req: Request) -> list[Request]:
    """Replace by id, or append. Returns a new list."""
    out = [r for r in requests if r.id != req.id]
    out.append(req)
    return out


def prune(requests: list[Request], *, keep_decided: int = 50, now: dt.datetime | None = None) -> list[Request]:
    """Drop expired approvals and trim decided history.

    Metadata is a JSON blob on a row, not a table -- it grows forever if
    nothing trims it, and every read of the team carries it. Pending requests
    are always kept; decided ones are capped at the most recent `keep_decided`
    so the audit trail survives a pilot without the blob becoming the largest
    thing in the database.
    """
    pending, decided = [], []
    for r in requests:
        if r.state == STATE_PENDING:
            pending.append(r)
        elif r.is_expired(now=now):
            # An approval nobody exercised. Dropping it is the point of the
            # expiry: it has to be asked for again.
            continue
        else:
            decided.append(r)
    decided.sort(key=lambda r: r.decided_at or r.created_at)
    return pending + decided[-keep_decided:]

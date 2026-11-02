#!/usr/bin/env python3
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

"""Prove that no request can opt out of the prompt log.

Sends one request per known way of switching LiteLLM's logging off through
a live proxy, waits for its logging worker, then reads the prompt log
directory and says whether each request landed with its content intact.
Any request the proxy accepted must be in the log, unredacted; one the
proxy refused need not be. Exit status 1 if that does not hold.

    uv run bin/check-no-log.py                                  # make proxy, ./prompt-log
    sudo uv run bin/check-no-log.py --log-dir /x1/llmao/prompt-log   # on the gateway host

The proxy's URL and the master key come from config.yaml (litellm.base_url,
litellm.master_key), as llmao-smoke reads them; --key or LLMAO_KEY overrides
the key. The master key is LiteLLM's admin and may call any model.

The cases are the surface measured on LiteLLM 1.105.0 (litellm_hooks.py,
"What a client can send"). Run it after a LiteLLM upgrade: a new version
can add a switch, or stop rejecting one it rejects today. A case that
starts failing is a hole in the terms of service, not a flaky test.
"""

from __future__ import annotations

import argparse
import gzip
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
import yaml
from easydict import EasyDict as edict  # noqa: N813

CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"

# Each case is one attempt to opt out: what goes in the body, what goes in
# the headers. The request's `user` field carries the case's tag, and the
# message content carries it too, so the log can be searched for both.
CASES = [
    edict(name="plain", body={}, headers={}),
    edict(name="body_no_log", body={"no-log": True}, headers={}),
    edict(name="stream_no_log", body={"no-log": True, "stream": True}, headers={}),
    edict(name="meta_no_log", body={"metadata": {"no-log": True}}, headers={}),
    edict(name="body_turn_off", body={"turn_off_message_logging": True}, headers={}),
    edict(name="meta_turn_off", body={"metadata": {"turn_off_message_logging": True}}, headers={}),
    edict(name="litellm_meta_turn_off", body={"litellm_metadata": {"turn_off_message_logging": True}}, headers={}),
    edict(name="hdr_enable_redaction", body={}, headers={"litellm-enable-message-redaction": "true"}),
    edict(name="hdr_x_enable_redaction", body={}, headers={"x-litellm-enable-message-redaction": "true"}),
    edict(name="hdr_disable_callbacks", body={}, headers={"x-litellm-disable-callbacks": "prompt_log"}),
    edict(name="hdr_disable_callbacks_class", body={}, headers={"x-litellm-disable-callbacks": "PromptLog"}),
]

REDACTED = "redacted-by-litellm"


def send(client: httpx.Client, model: str, run: str, case: edict) -> edict:
    tag = f"{case.name}-{run}"
    body = dict(model=model, user=tag, messages=[dict(role="user", content=f"hello from {tag}")], **case.body)
    resp = client.post("/v1/chat/completions", json=body, headers=case.headers)
    return edict(name=case.name, tag=tag, status=resp.status_code)


def log_lines(log_dir: Path):
    for path in sorted(log_dir.iterdir()):
        if path.suffix == ".gz":
            with gzip.open(path, "rt") as f:
                yield from f
        elif path.suffix == ".jsonl":
            with path.open() as f:
                yield from f


def verdict(sent: edict, lines: list[str]) -> str:
    mine = [line for line in lines if sent.tag in line]
    if any(f"hello from {sent.tag}" in line for line in mine):
        return "logged"
    if sent.status // 100 != 2:
        return "refused"
    return "redacted" if mine else "MISSING"


def first_model(client: httpx.Client) -> str:
    return client.get("/v1/models").json()["data"][0]["id"]


def litellm_section(path: Path) -> edict:
    """config.yaml's litellm section, or empty when the file is absent."""
    if not path.exists():
        return edict()
    return edict((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("litellm") or {})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--config", type=Path, default=CONFIG, help="config.yaml for the proxy URL and master key")
    ap.add_argument("--url", help="the proxy (default: config.yaml litellm.base_url)")
    ap.add_argument("--key", help="a virtual key or the master key (default: LLMAO_KEY, else config.yaml)")
    ap.add_argument("--log-dir", type=Path, default=Path("prompt-log"), help="the prompt log (default: %(default)s)")
    ap.add_argument("--model", help="model to call (default: the first the proxy lists)")
    ap.add_argument("--wait", type=float, default=3.0, help="seconds for the logging worker (default: %(default)s)")
    args = ap.parse_args()

    litellm = litellm_section(args.config)
    url = args.url or litellm.get("base_url") or "http://127.0.0.1:4000"
    key = args.key or os.environ.get("LLMAO_KEY") or litellm.get("master_key")
    if not key or key.startswith("CHANGE_ME"):
        raise SystemExit(f"no key: pass --key, set LLMAO_KEY, or put litellm.master_key in {args.config}")

    run = uuid.uuid4().hex[:8]
    with httpx.Client(base_url=url, headers={"Authorization": f"Bearer {key}"}, timeout=60) as client:
        model = args.model or first_model(client)
        sent = [send(client, model, run, case) for case in CASES]
    time.sleep(args.wait)
    lines = list(log_lines(args.log_dir))

    bad = 0
    for s in sent:
        v = verdict(s, lines)
        bad += v not in ("logged", "refused")
        print(f"{s.name:30} {s.status}  {v}")
    print(f"\n{len(sent) - bad} of {len(sent)} accounted for; model {model}, run {run}, {len(lines)} log lines read")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

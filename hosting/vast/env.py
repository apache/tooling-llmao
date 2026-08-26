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

"""Inspect and set Vast.ai instance env (account / template / instance).

Operator laptop tool. The GPU box does not run this.

  python3 hosting/vast/env.py list
  python3 hosting/vast/env.py show INSTANCE_ID
  python3 hosting/vast/env.py set INSTANCE_ID --vllm-set SET_ID
"""
from __future__ import annotations

import argparse
import logging
import shlex
import sys
from pathlib import Path
from typing import Any

import yaml
from easydict import EasyDict as edict

import vastai  # pip install vastai
from vastai import VastAI

THIS_DIR = Path(__file__).resolve().parent
REPO = THIS_DIR.parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from llmao.fleet import UnknownSet, config_for_set  # noqa: E402
from llmao.models import load_model_list  # noqa: E402

_LOGGER = logging.getLogger(__name__)


def reraise_vast_http(exc: BaseException) -> None:
    """Fail-fast with HTTP status + body (SDK raise_for_status omits the body)."""
    resp = getattr(exc, "response", None)
    if resp is None:
        raise
    status = getattr(resp, "status_code", "?")
    body = getattr(resp, "text", None)
    if body is None:
        raw = getattr(resp, "content", b"")
        body = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    raise SystemExit(f"Vast HTTP {status}: {body}") from exc


def parse_docker_env(raw: Any) -> dict[str, str]:
    """Template `env` is a docker-flag string; instance extra_env is a dict."""
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    tokens = shlex.split(str(raw))
    out: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-e" and i + 1 < len(tokens):
            key, _, val = tokens[i + 1].partition("=")
            if key:
                out[key] = val
            i += 2
            continue
        if tok.startswith("-e") and len(tok) > 2:
            key, _, val = tok[2:].partition("=")
            if key:
                out[key] = val
            i += 1
            continue
        if tok == "-p" and i + 1 < len(tokens):
            out[f"-p {tokens[i + 1]}"] = "1"
            i += 2
            continue
        if tok.startswith("-p") and len(tok) > 2:
            out[tok if tok.startswith("-p ") else f"-p {tok[2:]}"] = "1"
        i += 1
    return out


def looks_like_docker_options(raw: Any) -> bool:
    if not isinstance(raw, str) or not raw.strip():
        return False
    tokens = shlex.split(raw)
    return any(t == "-e" or t == "-p" or t.startswith("-e") or t.startswith("-p") for t in tokens)


def format_docker_options(env: dict[str, str]) -> str:
    parts = []
    for key, val in env.items():
        if key.startswith("-p"):
            parts.append(key)
        else:
            parts.append(f"-e {key}={shlex.quote(val)}")
    if not parts:
        raise SystemExit("no docker options to send")
    return " ".join(parts)


def image_args_of(row: dict) -> str:
    raw = row.get("image_args")
    if raw is None:
        raw = row.get("args")
    if raw is None:
        return ""
    if isinstance(raw, list):
        return " ".join(str(x) for x in raw)
    return str(raw)


def docker_options_env(row: dict) -> dict[str, str]:
    """Env that docker actually gets: options string plus extra_env overlay."""
    out: dict[str, str] = {}
    raw = image_args_of(row)
    if looks_like_docker_options(raw):
        out.update(parse_docker_env(raw))
    out.update(extra_env_of(row))
    return out


def load_cfg(path: Path) -> edict:
    if not path.is_file():
        raise SystemExit(
            f"Missing {path}. Copy config.yaml.example to config.yaml and edit secrets."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a mapping")
    return edict(data)


def fleet_key_from_cfg(cfg: edict) -> str:
    fleet = getattr(cfg, "fleet", None)
    key = ""
    if fleet is not None:
        key = str(getattr(fleet, "key", "") or "")
    key = key.strip()
    if not key or key.startswith("CHANGE_ME"):
        raise SystemExit("fleet.key is not configured in config.yaml")
    return key


def require_instance(vast: VastAI, instance_id: int) -> dict:
    row = vast.show_instance(id=instance_id)
    if not row:
        raise SystemExit(f"unknown instance: {instance_id}")
    return row


def extra_env_of(row: dict) -> dict[str, str]:
    extra = row.get("extra_env") or {}
    if isinstance(extra, list):
        extra = {pair[0]: pair[1] for pair in extra}
    if not isinstance(extra, dict):
        raise SystemExit(f"instance extra_env is not a mapping: {type(extra)}")
    return {str(k): "" if v is None else str(v) for k, v in extra.items()}


def template_hash_of(row: dict) -> str:
    return str(
        row.get("template_hash_id")
        or row.get("template_hash")
        or row.get("hash_id")
        or ""
    ).strip()


def template_env(vast: VastAI, hash_id: str) -> tuple[dict[str, str], dict | None]:
    if not hash_id:
        return {}, None
    templates = vast.search_templates(query={"hash_id": {"eq": hash_id}})
    if not templates:
        return {}, None
    tmpl = templates[0]
    return parse_docker_env(tmpl.get("env")), tmpl


def _print_table(headers: tuple[str, ...], records: list[tuple[str, ...]]) -> None:
    widths = [len(h) for h in headers]
    for rec in records:
        for i, val in enumerate(rec):
            widths[i] = max(widths[i], len(val))
    sep = "  "

    def line(vals: tuple[str, ...]) -> str:
        return sep.join(f"{vals[i]:<{widths[i]}}" for i in range(len(headers))).rstrip()

    print(line(headers))
    for rec in records:
        print(line(rec))


def cmd_list(vast: VastAI) -> None:
    rows = vast.show_instances() or []
    records = []
    for row in rows:
        opts = docker_options_env(row)
        records.append((
            str(row.get("id") or ""),
            str(row.get("actual_status") or row.get("status") or row.get("cur_state") or ""),
            "✓" if opts.get("FLEET_KEY", "").strip() else "",
            opts.get("VLLM_SET", "") or "",
            template_hash_of(row),
            str(row.get("label") or ""),
        ))
    _print_table(("ID", "STATUS", "FLEET", "VLLM_SET", "TEMPLATE", "LABEL"), records)


def _print_env_block(title: str, env: dict[str, str]) -> None:
    print(f"--- {title} ---")
    if not env:
        print("(none)")
        return
    for key in sorted(env):
        print(f"{key}={env[key]}")


def cmd_show(vast: VastAI, instance_id: int) -> None:
    row = require_instance(vast, instance_id)
    account = vast.show_env_vars(show_values=True)
    if not isinstance(account, dict):
        raise SystemExit("account secrets response is not a mapping")
    extra = extra_env_of(row)
    hash_id = template_hash_of(row)
    t_env, tmpl = template_env(vast, hash_id)
    print(f"=== instance {instance_id} ===")
    print(f"label={row.get('label') or ''}")
    print(f"status={row.get('actual_status') or row.get('status') or ''}")
    print(f"template_hash_id={hash_id}")
    if tmpl and tmpl.get("name"):
        print(f"template_name={tmpl.get('name')}")
    print()
    _print_env_block("account (secrets)", {str(k): str(v) for k, v in account.items()})
    print()
    _print_env_block("template", t_env)
    print()
    _print_env_block("instance extra_env", extra)
    print()
    raw_args = image_args_of(row)
    print("--- docker args ---")
    print(raw_args or "(none)")
    parsed = parse_docker_env(raw_args) if looks_like_docker_options(raw_args) else {}
    if parsed:
        print()
        _print_env_block("docker args (parsed)", parsed)


def print_set_plan(set_id: str, cfg: edict) -> None:
    try:
        payload = config_for_set(set_id, entries=load_model_list(cfg=cfg), cfg=cfg)
    except UnknownSet:
        raise SystemExit(f"unknown model_set {set_id!r} (not in config.yaml fleet.sets)") from None
    print(f"set {set_id} will install (from fleet.sets / GET /vllm/config/{set_id}):")
    for srv in payload["servers"]:
        host = srv.get("host") or ""
        print(f"  {srv['name']}\t{srv['model']}\t{host}:{srv['port']}")


def confirm_reboot() -> bool:
    try:
        ans = input("Ready to reboot? [Y/n] ").strip()
    except EOFError:
        raise SystemExit("no tty for reboot prompt") from None
    if ans == "" or ans.lower() in ("y", "yes"):
        return True
    if ans.lower() in ("n", "no"):
        return False
    raise SystemExit(f"unrecognized answer: {ans}")


def cmd_set(
    vast: VastAI,
    instance_id: int,
    *,
    vllm_set: str,
    config_path: Path,
    confirm=None,
) -> None:
    cfg = load_cfg(config_path)
    key = fleet_key_from_cfg(cfg)
    print_set_plan(vllm_set, cfg)
    row = require_instance(vast, instance_id)
    image = str(row.get("image_uuid") or row.get("image") or "").strip()
    if not image:
        raise SystemExit(
            f"instance {instance_id} has no image_uuid; Vast update_template requires image"
        )
    merged = docker_options_env(row)
    merged["FLEET_KEY"] = key
    merged["VLLM_SET"] = vllm_set
    args = format_docker_options(merged)
    update_kw = {"id": instance_id, "args": args, "image": image}
    hash_id = template_hash_of(row)
    if hash_id:
        update_kw["template_hash_id"] = hash_id
    try:
        vast.update_instance(**update_kw)
    except Exception as e:
        reraise_vast_http(e)
    _LOGGER.info(f"set instance={instance_id} VLLM_SET={vllm_set}")
    if confirm is None:
        confirm = confirm_reboot
    if confirm():
        try:
            vast.reboot_instance(id=instance_id)
        except Exception as e:
            reraise_vast_http(e)
        _LOGGER.info(f"rebooted instance={instance_id}")
    else:
        _LOGGER.info(f"skipped reboot instance={instance_id}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Vast.ai instance env (account / template / instance)")
    sub = p.add_subparsers(dest="cmd")
    p.set_defaults(cmd="list")

    sub.add_parser("list", help="list instances")

    show = sub.add_parser("show", help="print account, template, and instance env")
    show.add_argument("instance_id", type=int)

    setp = sub.add_parser("set", help="set FLEET_KEY (from config.yaml) and VLLM_SET")
    setp.add_argument("instance_id", type=int)
    setp.add_argument("--vllm-set", required=True, help="set id from config.yaml fleet.sets")
    setp.add_argument(
        "--config",
        type=Path,
        default=REPO / "config.yaml",
        help="path to config.yaml (fleet.key)",
    )
    return p


def main(argv: list[str] | None = None, vast: VastAI | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        style="{",
        format="[{levelname}|{name}] {message}",
    )
    args = build_parser().parse_args(argv)
    client = vast if vast is not None else VastAI()
    if args.cmd == "list":
        cmd_list(client)
    elif args.cmd == "show":
        cmd_show(client, args.instance_id)
    elif args.cmd == "set":
        cmd_set(
            client,
            args.instance_id,
            vllm_set=args.vllm_set,
            config_path=args.config,
        )
    else:
        raise SystemExit(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    main()

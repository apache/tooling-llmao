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

"""JSON API routes for llmao (STeVe-style module registration)."""

from __future__ import annotations

import functools
import hmac
import logging

import asfquart
import asfquart.auth
from asfquart.auth import Requirements as R
from quart import jsonify, request

from llmao.auth import current_identity
from llmao.fleet import UnknownSet, config_for_set
from llmao.seam import AuthzError

APP = asfquart.APP
_LOGGER = logging.getLogger(__name__)


class HttpError(Exception):
    """Expected HTTP error from an API handler (not AuthzError)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _ok_extras(result) -> str:
    """Short success extras from a JSON-ish dict; never includes secrets."""
    if not isinstance(result, dict):
        return ''
    parts = []
    if result.get('set_id') is not None:
        parts.append(f'set_id={result["set_id"]}')
    servers = result.get('servers')
    if isinstance(servers, list):
        models = ','.join(
            f'{s.get("name")}:{s.get("port")}' if isinstance(s, dict) else str(s)
            for s in servers
        )
        parts.append(f'models={models}')
    if result.get('project') is not None:
        parts.append(f'project={result["project"]}')
    if 'count' in result:
        parts.append(f'count={result["count"]}')
    if 'provisioned' in result:
        parts.append(f'provisioned={result["provisioned"]}')
    return (' ' + ' '.join(parts)) if parts else ''


def api(fn):
    """JSON handlers: jsonify the return value; AuthzError → 403; else log + 500."""

    def err(status, message):
        body = {"error": {"message": message, "type": "llmao_error", "code": status}}
        return jsonify(body), status

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            result = await fn(*args, **kwargs)
        except AuthzError as e:
            _LOGGER.warning(f'{fn.__name__}: {e}')
            return err(403, str(e))
        except HttpError as e:
            msg = f'{fn.__name__}: {e.status} {e}'
            if e.status >= 500:
                _LOGGER.error(msg)
            else:
                _LOGGER.warning(msg)
            return err(e.status, str(e))
        except Exception:
            _LOGGER.exception(f'unhandled error in {fn.__name__}')
            return err(500, 'internal error in gateway; check the server log')
        if fn.__name__ == 'healthz':
            _LOGGER.debug(f'{fn.__name__} ok')
        else:
            _LOGGER.info(f'{fn.__name__} ok{_ok_extras(result)}')
        return jsonify(result)

    return wrapper


@APP.get("/healthz")
@api
async def healthz():
    return {"status": "ok"}


def _fleet_key() -> str:
    fleet = getattr(APP.cfg, "fleet", None)
    key = ""
    if fleet is not None:
        key = str(getattr(fleet, "key", "") or "")
    return key.strip()


@APP.get("/vllm/config/<set_id>")
@api
async def vllm_config(set_id: str):
    """JSON for one model_set. Bearer fleet key; no OAuth (GPU boxes)."""
    expected = _fleet_key()
    if not expected or expected.startswith("CHANGE_ME"):
        raise HttpError(503, "fleet.key is not configured")
    auth = request.headers.get("Authorization") or ""
    prefix = "Bearer "
    if not auth.startswith(prefix):
        raise HttpError(403, "missing fleet key")
    presented = auth[len(prefix):].strip()
    if not hmac.compare_digest(presented, expected):
        raise HttpError(403, "invalid fleet key")
    try:
        payload = config_for_set(set_id, cfg=APP.cfg)
    except UnknownSet:
        raise HttpError(404, f"unknown model_set: {set_id}")
    APP.fleet.note_config_fetch(set_id)
    return payload


@APP.get("/v1/projects/<project>/usage")
@asfquart.auth.require
@api
async def project_usage(project: str):
    seam = APP.config["LLMAO_SEAM"]
    ident = await current_identity(APP.cfg)
    rows = await seam.project_activity(ident, project)
    total = round(sum(r.get("cost_usd", 0.0) for r in rows), 6)
    return {
        "project": project,
        "entries": rows,
        "total_cost_usd": total,
        "count": len(rows),
    }


@APP.get("/v1/projects/<project>/budget")
@asfquart.auth.require({R.committer})
@api
async def project_budget(project: str):
    seam = APP.config["LLMAO_SEAM"]
    ident = await current_identity(APP.cfg)
    info = await seam.team_status(ident, project)
    if info is None:
        return {"project": project, "provisioned": False}
    return {
        "project": project,
        "provisioned": True,
        "max_budget_usd": info.max_budget,
        "spend_usd": info.spend,
        "remaining_usd": round(max(0.0, info.max_budget - info.spend), 6),
    }
